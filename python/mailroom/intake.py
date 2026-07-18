"""Provider-neutral intake contract and read-only Maton Graph delta adapter."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

from .ledger import MailroomLedger
from .models import IncomingMessage, IntakeBatch, IntakeEvent


MATON_GRAPH_ROOT = "https://gateway.maton.ai/outlook/v1.0"
MAIL_SELECT = ",".join([
    "id", "internetMessageId", "conversationId", "parentFolderId", "receivedDateTime",
    "sentDateTime", "subject", "from", "toRecipients", "ccRecipients", "bccRecipients",
    "bodyPreview", "body", "isRead", "isDraft", "hasAttachments", "changeKey",
])


class IntakeError(RuntimeError):
    pass


class StaleCheckpointError(IntakeError):
    pass


class MailIntake(Protocol):
    def poll(self) -> IntakeBatch: ...


FetchJson = Callable[[str, dict[str, str]], dict[str, Any]]


@dataclass
class MatonDeltaIntake:
    ledger: MailroomLedger
    mailbox: str
    connection_id: str
    api_key: str
    folder: str = "inbox"
    bootstrap_lookback_hours: int = 2
    max_pages: int = 100
    fetch_json: FetchJson | None = None
    prefer_immutable_ids: bool = True
    use_checkpoint: bool = True

    @property
    def adapter_name(self) -> str:
        return "maton_delta"

    def poll(self) -> IntakeBatch:
        cursor = (
            self.ledger.get_checkpoint(self.adapter_name, self.mailbox, self.folder)
            if self.use_checkpoint else None
        )
        bootstrap_cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=self.bootstrap_lookback_hours)
            if cursor is None else None
        )
        url = cursor or self._initial_url()
        events: list[IntakeEvent] = []
        final_delta: str | None = None
        pages = 0
        fetch = self.fetch_json or self._fetch_json

        try:
            while url and pages < self.max_pages:
                try:
                    data = fetch(self._gateway_url(url), self._headers())
                except urllib.error.HTTPError as exc:
                    # Maton's Outlook proxy currently returns 401 for Graph's
                    # IdType preference even when the same credentials work
                    # without it. Retry once and stop labeling IDs immutable.
                    if exc.code != 401 or not self.prefer_immutable_ids:
                        raise
                    self.prefer_immutable_ids = False
                    data = fetch(self._gateway_url(url), self._headers())
                for item in data.get("value", []):
                    removed = item.get("@removed")
                    if removed is not None:
                        events.append(IntakeEvent(
                            event_type="removed",
                            mailbox=self.mailbox,
                            provider_message_id=item.get("id"),
                            removal_reason=removed.get("reason"),
                        ))
                        continue
                    if bootstrap_cutoff:
                        received_value = item.get("receivedDateTime") or item.get("sentDateTime")
                        if received_value:
                            try:
                                received = datetime.fromisoformat(
                                    str(received_value).replace("Z", "+00:00")
                                )
                            except ValueError:
                                received = None
                            if received is not None:
                                if received.tzinfo is None:
                                    received = received.replace(tzinfo=timezone.utc)
                                if received < bootstrap_cutoff:
                                    continue
                    if self._requires_hydration(item):
                        try:
                            item = self._hydrate_item(item, fetch)
                        except urllib.error.HTTPError as exc:
                            # A message can disappear between the delta page and
                            # the hydration GET. Treat that as a removal event so
                            # this valid delta page can still advance its cursor;
                            # otherwise every cycle is pinned to the same stale
                            # checkpoint forever.
                            if exc.code not in (404, 410):
                                raise
                            events.append(IntakeEvent(
                                event_type="removed",
                                mailbox=self.mailbox,
                                provider_message_id=item.get("id"),
                                removal_reason=f"hydration_http_{exc.code}",
                            ))
                            continue
                    message = self._to_message(item)
                    if bootstrap_cutoff:
                        timestamp_status = self._bootstrap_timestamp_status(message, bootstrap_cutoff)
                        if timestamp_status == "outside":
                            continue
                        if timestamp_status == "unknown":
                            message = replace(
                                message,
                                intake_warnings=("UNKNOWN_RECEIVED_AT_DURING_BOOTSTRAP",),
                            )
                    events.append(IntakeEvent(event_type="upsert", mailbox=self.mailbox, message=message))
                url = self._gateway_url(data.get("@odata.nextLink"))
                if not url:
                    final_delta = self._gateway_url(data.get("@odata.deltaLink"))
                pages += 1
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 404, 410) and cursor:
                raise StaleCheckpointError(f"Maton delta checkpoint rejected: HTTP {exc.code}") from exc
            raise IntakeError(f"Maton delta failed: HTTP {exc.code}") from exc
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise IntakeError(f"Maton delta failed: {exc}") from exc

        if url:
            raise IntakeError(f"Maton delta exceeded max_pages={self.max_pages}; checkpoint not advanced")
        if not final_delta:
            raise IntakeError("Maton delta completed without @odata.deltaLink; checkpoint not advanced")
        return IntakeBatch(
            events=tuple(events), checkpoint=final_delta, previous_checkpoint=cursor,
            adapter=self.adapter_name,
            mailbox=self.mailbox, scope=self.folder,
        )

    def _initial_url(self) -> str:
        since = datetime.now(timezone.utc) - timedelta(hours=self.bootstrap_lookback_hours)
        params = {
            "$select": MAIL_SELECT,
            "$filter": f"receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            "$top": "50",
        }
        return f"{MATON_GRAPH_ROOT}/me/mailFolders/{urllib.parse.quote(self.folder)}/messages/delta?{urllib.parse.urlencode(params)}"

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Maton-Connection": self.connection_id,
            "Accept": "application/json",
        }
        if self.prefer_immutable_ids:
            headers["Prefer"] = 'IdType="ImmutableId"'
        return headers

    @staticmethod
    def _requires_hydration(item: dict[str, Any]) -> bool:
        return any(
            key not in item
            for key in ("conversationId", "receivedDateTime", "subject", "from", "body")
        )

    def _hydrate_item(self, partial: dict[str, Any], fetch: FetchJson) -> dict[str, Any]:
        message_id = partial.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise ValueError("Partial delta message is missing id")
        params = urllib.parse.urlencode({"$select": MAIL_SELECT})
        url = (
            f"{MATON_GRAPH_ROOT}/me/messages/"
            f"{urllib.parse.quote(message_id, safe='')}?{params}"
        )
        full = fetch(url, self._headers())
        if not isinstance(full, dict) or full.get("id") != message_id:
            raise ValueError("Message hydration returned a malformed or mismatched object")
        hydrated = dict(full)
        hydrated.update(partial)
        return hydrated

    @staticmethod
    def _gateway_url(url: str | None) -> str | None:
        if not url:
            return None
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port:
            raise ValueError("Unsafe Maton continuation URL")
        if parsed.hostname == "graph.microsoft.com":
            return urllib.parse.urlunparse((
                "https", "gateway.maton.ai", f"/outlook{parsed.path}", "", parsed.query, "",
            ))
        if parsed.hostname == "gateway.maton.ai":
            return url
        raise ValueError(f"Untrusted Maton continuation host: {parsed.hostname or 'missing'}")

    @staticmethod
    def _fetch_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)

    def _to_message(self, item: dict[str, Any]) -> IncomingMessage:
        sender = (item.get("from") or {}).get("emailAddress") or {}
        body = item.get("body") or {}
        content_material = json.dumps(item, sort_keys=True, default=str).encode("utf-8")
        return IncomingMessage(
            mailbox=self.mailbox,
            provider_message_id=item["id"],
            immutable_id=item.get("id") if self.prefer_immutable_ids else None,
            internet_message_id=item.get("internetMessageId"),
            conversation_id=item.get("conversationId"),
            folder=self.folder,
            received_at=item.get("receivedDateTime") or item.get("sentDateTime"),
            sender_email=(sender.get("address") or "").lower() or None,
            sender_name=sender.get("name"),
            subject=item.get("subject"),
            body_preview=item.get("bodyPreview"),
            body_content=body.get("content"),
            is_read=item.get("isRead"),
            has_attachments=item.get("hasAttachments"),
            content_hash=hashlib.sha256(content_material).hexdigest(),
            raw=item,
        )

    @staticmethod
    def _bootstrap_timestamp_status(message: IncomingMessage, cutoff: datetime) -> str:
        if not message.received_at:
            return "unknown"
        try:
            received = datetime.fromisoformat(message.received_at.replace("Z", "+00:00"))
        except ValueError:
            return "unknown"
        if received.tzinfo is None:
            received = received.replace(tzinfo=timezone.utc)
        return "inside" if received >= cutoff else "outside"
