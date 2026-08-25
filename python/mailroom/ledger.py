"""Durable SQLite ledger and audited Mailroom state machine."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .models import Disposition, IncomingMessage, MailState, Priority, RouteDecision


DEFAULT_DB = Path.home() / ".openclaw" / "mailroom" / "mailroom.db"
_UNSET = object()


ALLOWED_TRANSITIONS: dict[MailState, set[MailState]] = {
    MailState.INGESTED: {MailState.ROUTING_REVIEW, MailState.ROUTED, MailState.DROPPED, MailState.ERROR},
    MailState.ROUTING_REVIEW: {MailState.ROUTED, MailState.DROPPED, MailState.ERROR},
    MailState.ROUTED: {MailState.DRAFT_REQUESTED, MailState.DROPPED, MailState.DEFERRED, MailState.DENIED_MESSAGE, MailState.REPLIED_ELSEWHERE, MailState.ERROR},
    MailState.DRAFT_REQUESTED: {MailState.DRAFTING, MailState.REPLIED_ELSEWHERE, MailState.ERROR},
    MailState.DRAFTING: {MailState.DRAFT_REQUESTED, MailState.DRAFT_PROPOSED, MailState.DROPPED, MailState.REPLIED_ELSEWHERE, MailState.ERROR},
    MailState.DRAFT_PROPOSED: {MailState.DRAFT_REQUESTED, MailState.REVISION_REQUESTED, MailState.DEFERRED, MailState.DENIED_MESSAGE, MailState.REPLIED_ELSEWHERE, MailState.OUTLOOK_DRAFTING, MailState.CANCELLED, MailState.ERROR},
    MailState.REVISION_REQUESTED: {MailState.DRAFT_REQUESTED, MailState.DRAFTING, MailState.DRAFT_PROPOSED, MailState.REPLIED_ELSEWHERE, MailState.OUTLOOK_DRAFTED, MailState.CANCELLED, MailState.ERROR},
    MailState.DEFERRED: {MailState.ROUTED, MailState.DRAFT_PROPOSED, MailState.SEND_APPROVAL_PENDING, MailState.DENIED_MESSAGE, MailState.CANCELLED},
    MailState.OUTLOOK_DRAFTING: {MailState.OUTLOOK_DRAFTED, MailState.REPLIED_ELSEWHERE, MailState.ERROR},
    MailState.OUTLOOK_DRAFTED: {MailState.SEND_APPROVAL_PENDING, MailState.REVISION_REQUESTED, MailState.DEFERRED, MailState.CANCELLED, MailState.ERROR},
    MailState.SEND_APPROVAL_PENDING: {MailState.SENDING, MailState.REVISION_REQUESTED, MailState.DEFERRED, MailState.REPLIED_ELSEWHERE, MailState.CANCELLED, MailState.ERROR},
    MailState.SENDING: {MailState.SEND_APPROVAL_PENDING, MailState.SEND_ACCEPTED, MailState.SEND_OUTCOME_UNKNOWN, MailState.REPLIED_ELSEWHERE, MailState.ERROR},
    MailState.SEND_ACCEPTED: {MailState.SENT_VERIFIED, MailState.SEND_OUTCOME_UNKNOWN, MailState.ERROR},
    MailState.SEND_OUTCOME_UNKNOWN: {MailState.SEND_ACCEPTED, MailState.SENT_VERIFIED, MailState.ERROR},
    MailState.ERROR: {MailState.ROUTED, MailState.DRAFT_REQUESTED, MailState.SEND_APPROVAL_PENDING, MailState.CANCELLED},
}


class InvalidTransition(ValueError):
    pass


class ConcurrentUpdate(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _message_hash(message: IncomingMessage) -> str:
    if message.content_hash:
        return message.content_hash
    material = "\n".join([
        message.mailbox.lower(),
        message.internet_message_id or "",
        message.conversation_id or "",
        message.received_at or "",
        (message.sender_email or "").lower(),
        message.subject or "",
        message.body_content or message.body_preview or "",
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class MailroomLedger:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path).expanduser()
        parent_existed = self.path.parent.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not parent_existed or self.path.parent == DEFAULT_DB.parent:
            self.path.parent.chmod(0o700)
        self.initialize()
        self._harden_permissions()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        self._harden_permissions()
        return conn

    def _harden_permissions(self) -> None:
        for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
            if path.exists():
                path.chmod(0o600)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS mail_items (
                    mail_item_id TEXT PRIMARY KEY,
                    callback_token TEXT NOT NULL UNIQUE,
                    mailbox TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    immutable_id TEXT,
                    internet_message_id TEXT,
                    conversation_id TEXT,
                    folder TEXT NOT NULL DEFAULT 'inbox',
                    received_at TEXT,
                    sender_email TEXT,
                    sender_name TEXT,
                    subject TEXT,
                    body_preview TEXT,
                    body_content TEXT,
                    content_hash TEXT NOT NULL,
                    is_read INTEGER,
                    has_attachments INTEGER,
                    attachments_json TEXT,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    state TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'unclassified',
                    priority TEXT NOT NULL DEFAULT 'P3',
                    importance TEXT NOT NULL DEFAULT 'unknown',
                    urgency TEXT NOT NULL DEFAULT 'unknown',
                    triage_action TEXT,
                    triage_model TEXT,
                    triage_confidence REAL,
                    triage_rationale TEXT,
                    draft_owner TEXT,
                    watchers_json TEXT NOT NULL DEFAULT '[]',
                    route_confidence REAL,
                    route_reasons_json TEXT NOT NULL DEFAULT '[]',
                    deferred_until TEXT,
                    deferred_from_state TEXT,
                    denied_content_hash TEXT,
                    outlook_draft_id TEXT,
                    approval_fingerprint TEXT,
                    send_accepted_at TEXT,
                    sent_message_id TEXT,
                    replied_sent_id TEXT,
                    replied_sent_at TEXT,
                    reply_target_message_id TEXT,
                    reply_target_received_at TEXT,
                    reply_target_sender_email TEXT,
                    related_messages_json TEXT,
                    conversation_messages_json TEXT,
                    new_email_checked_at TEXT,
                    proposal_json TEXT,
                    card_channel TEXT,
                    card_account_id TEXT,
                    card_chat_id TEXT,
                    card_thread_id TEXT,
                    card_message_id TEXT,
                    last_error TEXT,
                    run_mode TEXT NOT NULL DEFAULT 'shadow',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(mailbox, provider_message_id)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_immutable
                    ON mail_items(mailbox, immutable_id)
                    WHERE immutable_id IS NOT NULL AND immutable_id != '';
                CREATE UNIQUE INDEX IF NOT EXISTS uq_mail_internet_id
                    ON mail_items(mailbox, internet_message_id)
                    WHERE internet_message_id IS NOT NULL AND internet_message_id != '';
                CREATE INDEX IF NOT EXISTS ix_mail_state ON mail_items(state, updated_at);
                CREATE INDEX IF NOT EXISTS ix_mail_conversation ON mail_items(mailbox, conversation_id, received_at);

                CREATE TABLE IF NOT EXISTS mail_events (
                    event_id TEXT PRIMARY KEY,
                    mail_item_id TEXT NOT NULL REFERENCES mail_items(mail_item_id),
                    event_type TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT,
                    actor TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_mail_events_item ON mail_events(mail_item_id, created_at);

                CREATE TABLE IF NOT EXISTS adopted_provider_messages (
                    mailbox TEXT NOT NULL,
                    provider_message_id TEXT NOT NULL,
                    mail_item_id TEXT NOT NULL REFERENCES mail_items(mail_item_id),
                    adopted_at TEXT NOT NULL,
                    PRIMARY KEY(mailbox, provider_message_id)
                );
                CREATE INDEX IF NOT EXISTS ix_adopted_mail_item
                    ON adopted_provider_messages(mail_item_id);

                CREATE TABLE IF NOT EXISTS thread_policies (
                    mailbox TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    policy TEXT NOT NULL,
                    route_owner TEXT,
                    reason TEXT,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(mailbox, conversation_id)
                );

                CREATE TABLE IF NOT EXISTS pending_inputs (
                    pending_input_id TEXT PRIMARY KEY,
                    mail_item_id TEXT NOT NULL REFERENCES mail_items(mail_item_id),
                    kind TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    account_id TEXT,
                    chat_id TEXT NOT NULL,
                    card_message_id TEXT NOT NULL,
                    expected_sender_id TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    resolved_at TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_pending_lookup
                    ON pending_inputs(channel, chat_id, card_message_id, resolved_at);

                CREATE TABLE IF NOT EXISTS intake_checkpoints (
                    adapter TEXT NOT NULL,
                    mailbox TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    cursor TEXT,
                    last_success_at TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(adapter, mailbox, scope)
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(mail_items)")}
            migrations = {
                "run_mode": "ALTER TABLE mail_items ADD COLUMN run_mode TEXT NOT NULL DEFAULT 'shadow'",
                "proposal_json": "ALTER TABLE mail_items ADD COLUMN proposal_json TEXT",
                "card_channel": "ALTER TABLE mail_items ADD COLUMN card_channel TEXT",
                "card_account_id": "ALTER TABLE mail_items ADD COLUMN card_account_id TEXT",
                "card_chat_id": "ALTER TABLE mail_items ADD COLUMN card_chat_id TEXT",
                "card_thread_id": "ALTER TABLE mail_items ADD COLUMN card_thread_id TEXT",
                "card_message_id": "ALTER TABLE mail_items ADD COLUMN card_message_id TEXT",
                "deferred_from_state": "ALTER TABLE mail_items ADD COLUMN deferred_from_state TEXT",
                "send_accepted_at": "ALTER TABLE mail_items ADD COLUMN send_accepted_at TEXT",
                "sent_message_id": "ALTER TABLE mail_items ADD COLUMN sent_message_id TEXT",
                "replied_sent_id": "ALTER TABLE mail_items ADD COLUMN replied_sent_id TEXT",
                "replied_sent_at": "ALTER TABLE mail_items ADD COLUMN replied_sent_at TEXT",
                "attachments_json": "ALTER TABLE mail_items ADD COLUMN attachments_json TEXT",
                "reply_target_message_id": "ALTER TABLE mail_items ADD COLUMN reply_target_message_id TEXT",
                "reply_target_received_at": "ALTER TABLE mail_items ADD COLUMN reply_target_received_at TEXT",
                "reply_target_sender_email": "ALTER TABLE mail_items ADD COLUMN reply_target_sender_email TEXT",
                "related_messages_json": "ALTER TABLE mail_items ADD COLUMN related_messages_json TEXT",
                "conversation_messages_json": "ALTER TABLE mail_items ADD COLUMN conversation_messages_json TEXT",
                "new_email_checked_at": "ALTER TABLE mail_items ADD COLUMN new_email_checked_at TEXT",
                "importance": "ALTER TABLE mail_items ADD COLUMN importance TEXT NOT NULL DEFAULT 'unknown'",
                "urgency": "ALTER TABLE mail_items ADD COLUMN urgency TEXT NOT NULL DEFAULT 'unknown'",
                "triage_action": "ALTER TABLE mail_items ADD COLUMN triage_action TEXT",
                "triage_model": "ALTER TABLE mail_items ADD COLUMN triage_model TEXT",
                "triage_confidence": "ALTER TABLE mail_items ADD COLUMN triage_confidence REAL",
                "triage_rationale": "ALTER TABLE mail_items ADD COLUMN triage_rationale TEXT",
            }
            for column, statement in migrations.items():
                if column not in columns:
                    conn.execute(statement)
            policy_columns = {row["name"] for row in conn.execute("PRAGMA table_info(thread_policies)")}
            if "route_owner" not in policy_columns:
                conn.execute("ALTER TABLE thread_policies ADD COLUMN route_owner TEXT")

    def _find_existing(self, conn: sqlite3.Connection, message: IncomingMessage) -> sqlite3.Row | None:
        for column, value in (
            ("immutable_id", message.immutable_id),
            ("internet_message_id", message.internet_message_id),
            ("provider_message_id", message.provider_message_id),
        ):
            if value:
                row = conn.execute(
                    f"SELECT * FROM mail_items WHERE mailbox = ? AND {column} = ?",  # noqa: S608 - column is internal constant
                    (message.mailbox, value),
                ).fetchone()
                if row:
                    return row
        return None

    @staticmethod
    def _find_adopted(
        conn: sqlite3.Connection, mailbox: str, provider_message_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT mail_items.* FROM adopted_provider_messages
            JOIN mail_items USING (mail_item_id)
            WHERE adopted_provider_messages.mailbox = ?
              AND adopted_provider_messages.provider_message_id = ?
            """,
            (mailbox, provider_message_id),
        ).fetchone()

    def upsert_message(
        self,
        message: IncomingMessage,
        *,
        actor: str = "intake",
        run_mode: str = "shadow",
    ) -> tuple[dict[str, Any], bool]:
        if run_mode not in {"shadow", "production"}:
            raise ValueError(f"Unsupported run_mode: {run_mode}")
        now = utcnow()
        content_hash = _message_hash(message)
        with self.transaction() as conn:
            adopted = self._find_adopted(
                conn, message.mailbox, message.provider_message_id,
            )
            if adopted:
                self._event(
                    conn, adopted["mail_item_id"], "ADOPTED_MESSAGE_RESEEN",
                    adopted["state"], adopted["state"], actor,
                    {"provider_message_id": message.provider_message_id},
                )
                return dict(adopted), False
            existing = self._find_existing(conn, message)
            if existing:
                try:
                    prior_raw = json.loads(existing["raw_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    prior_raw = {}
                merged_raw = dict(prior_raw) if isinstance(prior_raw, dict) else {}
                merged_raw.update(message.raw)
                merged_message = replace(
                    message,
                    immutable_id=message.immutable_id or existing["immutable_id"],
                    internet_message_id=message.internet_message_id or existing["internet_message_id"],
                    conversation_id=message.conversation_id or existing["conversation_id"],
                    received_at=message.received_at or existing["received_at"],
                    sender_email=message.sender_email or existing["sender_email"],
                    sender_name=message.sender_name or existing["sender_name"],
                    subject=message.subject if message.subject is not None else existing["subject"],
                    body_preview=(
                        message.body_preview
                        if message.body_preview is not None else existing["body_preview"]
                    ),
                    body_content=(
                        message.body_content
                        if message.body_content is not None else existing["body_content"]
                    ),
                    is_read=message.is_read if message.is_read is not None else existing["is_read"],
                    has_attachments=(
                        message.has_attachments
                        if message.has_attachments is not None else existing["has_attachments"]
                    ),
                    content_hash=None,
                    raw=merged_raw,
                )
                content_hash = (
                    hashlib.sha256(
                        json.dumps(merged_raw, sort_keys=True, default=str).encode("utf-8")
                    ).hexdigest()
                    if message.content_hash else _message_hash(merged_message)
                )
                changed = existing["content_hash"] != content_hash
                conn.execute(
                    """
                    UPDATE mail_items SET
                        provider_message_id = ?, immutable_id = COALESCE(?, immutable_id),
                        internet_message_id = COALESCE(?, internet_message_id),
                        conversation_id = COALESCE(?, conversation_id), folder = ?,
                        received_at = COALESCE(?, received_at), sender_email = ?, sender_name = ?,
                        subject = ?, body_preview = ?, body_content = ?, content_hash = ?,
                        is_read = ?, has_attachments = ?, raw_json = ?, updated_at = ?,
                        version = version + 1
                    WHERE mail_item_id = ?
                    """,
                    (
                        merged_message.provider_message_id, merged_message.immutable_id,
                        merged_message.internet_message_id, merged_message.conversation_id,
                        merged_message.folder, merged_message.received_at,
                        merged_message.sender_email, merged_message.sender_name,
                        merged_message.subject, merged_message.body_preview,
                        merged_message.body_content, content_hash, merged_message.is_read,
                        merged_message.has_attachments, _json(merged_raw), now,
                        existing["mail_item_id"],
                    ),
                )
                self._event(
                    conn, existing["mail_item_id"], "MESSAGE_UPDATED" if changed else "MESSAGE_RESEEN",
                    existing["state"], existing["state"], actor,
                    {"content_changed": changed},
                )
                row = conn.execute(
                    "SELECT * FROM mail_items WHERE mail_item_id = ?", (existing["mail_item_id"],)
                ).fetchone()
                return dict(row), False

            mail_item_id = str(uuid.uuid4())
            callback_token = secrets.token_urlsafe(16)
            conn.execute(
                """
                INSERT INTO mail_items (
                    mail_item_id, callback_token, mailbox, provider_message_id, immutable_id,
                    internet_message_id, conversation_id, folder, received_at, sender_email,
                    sender_name, subject, body_preview, body_content, content_hash, is_read,
                    has_attachments, raw_json, state, run_mode, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mail_item_id, callback_token, message.mailbox, message.provider_message_id,
                    message.immutable_id, message.internet_message_id, message.conversation_id,
                    message.folder, message.received_at, message.sender_email, message.sender_name,
                    message.subject, message.body_preview, message.body_content, content_hash,
                    message.is_read, message.has_attachments, _json(message.raw),
                    MailState.INGESTED.value, run_mode, now, now,
                ),
            )
            self._event(conn, mail_item_id, "MESSAGE_INGESTED", None, MailState.INGESTED.value, actor, {})
            row = conn.execute("SELECT * FROM mail_items WHERE mail_item_id = ?", (mail_item_id,)).fetchone()
            return dict(row), True

    def find_existing_message(self, message: IncomingMessage) -> dict[str, Any] | None:
        """Find a deduplicated ledger row without updating it."""
        with self.connect() as conn:
            row = self._find_existing(conn, message)
            return dict(row) if row else None

    def record_attachments(
        self,
        mail_item_id: str,
        attachments: list[dict[str, Any]],
        *,
        actor: str = "attachment-reader",
    ) -> dict[str, Any]:
        """Persist attachment display metadata without storing attachment contents."""
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            updated = conn.execute(
                """
                UPDATE mail_items SET attachments_json = ?, updated_at = ?, version = version + 1
                WHERE mail_item_id = ? AND version = ?
                """,
                (_json(attachments), utcnow(), mail_item_id, row["version"]),
            )
            if updated.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            self._event(
                conn, mail_item_id, "ATTACHMENTS_INDEXED", row["state"], row["state"], actor,
                {"count": len(attachments)},
            )
            return dict(self._get_row(conn, mail_item_id))

    def promote_catchup_message(
        self,
        message: IncomingMessage,
        decision: RouteDecision,
        *,
        actor: str = "catchup",
    ) -> tuple[dict[str, Any], bool]:
        """Promote one reviewed historical match into production exactly once."""
        if decision.outcome != "ROUTED" or not decision.draft_owner:
            raise ValueError("Catch-up promotion requires a routed owner")
        row, created = self.upsert_message(message, actor=actor, run_mode="production")
        if created:
            return self.route(row["mail_item_id"], decision, actor=actor), True
        if row["run_mode"] == "production":
            return row, False
        if row["state"] not in {
            MailState.INGESTED.value, MailState.ROUTED.value,
            MailState.ROUTING_REVIEW.value, MailState.DROPPED.value,
        }:
            raise InvalidTransition(
                f"Cannot promote shadow item {row['mail_item_id']} from {row['state']}"
            )
        now = utcnow()
        with self.transaction() as conn:
            current = self._get_row(conn, row["mail_item_id"])
            if current["run_mode"] == "production":
                return dict(current), False
            if current["version"] != row["version"]:
                raise ConcurrentUpdate(row["mail_item_id"])
            updated = conn.execute(
                """
                UPDATE mail_items SET run_mode = 'production', state = ?, disposition = ?,
                    priority = ?, importance = ?, urgency = ?, triage_action = ?,
                    triage_model = ?, triage_confidence = ?, triage_rationale = ?,
                    draft_owner = ?, watchers_json = ?, route_confidence = ?,
                    route_reasons_json = ?, updated_at = ?, version = version + 1
                WHERE mail_item_id = ? AND version = ? AND run_mode = 'shadow'
                """,
                (
                    MailState.ROUTED.value, decision.disposition.value, decision.priority.value,
                    decision.importance.value, decision.urgency.value, decision.triage_action,
                    decision.triage_model, decision.triage_confidence, decision.triage_rationale,
                    decision.draft_owner, _json(list(decision.watchers)), decision.confidence,
                    _json(list(decision.reasons)), now, row["mail_item_id"], row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrentUpdate(row["mail_item_id"])
            if message.conversation_id:
                conn.execute(
                    """
                    INSERT INTO thread_policies
                        (mailbox, conversation_id, policy, route_owner, reason, actor, created_at, updated_at)
                    VALUES (?, ?, 'ROUTE_OWNER', ?, 'Reviewed catch-up promotion', ?, ?, ?)
                    ON CONFLICT(mailbox, conversation_id) DO UPDATE SET
                        policy = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.policy ELSE excluded.policy END,
                        route_owner = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.route_owner ELSE excluded.route_owner END,
                        reason = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.reason ELSE excluded.reason END,
                        actor = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.actor ELSE excluded.actor END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        message.mailbox, message.conversation_id, decision.draft_owner,
                        actor, now, now,
                    ),
                )
            self._event(
                conn, row["mail_item_id"], "CATCHUP_PROMOTED", row["state"],
                MailState.ROUTED.value, actor,
                {"owner": decision.draft_owner, "reasons": list(decision.reasons)},
            )
            return dict(self._get_row(conn, row["mail_item_id"])), True

    def route(self, mail_item_id: str, decision: RouteDecision, *, actor: str = "router") -> dict[str, Any]:
        if decision.outcome == "DROPPED":
            target = MailState.DROPPED
        elif decision.outcome == "ROUTED":
            target = MailState.ROUTED
        else:
            target = MailState.ROUTING_REVIEW
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            self._assert_transition(MailState(row["state"]), target)
            updated = conn.execute(
                """
                UPDATE mail_items SET state = ?, disposition = ?, priority = ?, draft_owner = ?,
                    importance = ?, urgency = ?, triage_action = ?, triage_model = ?,
                    triage_confidence = ?, triage_rationale = ?,
                    watchers_json = ?, route_confidence = ?, route_reasons_json = ?,
                    updated_at = ?, version = version + 1
                WHERE mail_item_id = ? AND version = ?
                """,
                (
                    target.value, decision.disposition.value, decision.priority.value,
                    decision.draft_owner, decision.importance.value, decision.urgency.value,
                    decision.triage_action, decision.triage_model, decision.triage_confidence,
                    decision.triage_rationale, _json(list(decision.watchers)), decision.confidence,
                    _json(list(decision.reasons)), utcnow(), mail_item_id, row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            if (
                target == MailState.ROUTED
                and decision.draft_owner
                and row["conversation_id"]
                and row["run_mode"] == "production"
            ):
                now = utcnow()
                conn.execute(
                    """
                    INSERT INTO thread_policies
                        (mailbox, conversation_id, policy, route_owner, reason, actor, created_at, updated_at)
                    VALUES (?, ?, 'ROUTE_OWNER', ?, 'Automatic high-confidence route', ?, ?, ?)
                    ON CONFLICT(mailbox, conversation_id) DO UPDATE SET
                        policy = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.policy ELSE excluded.policy END,
                        route_owner = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.route_owner ELSE excluded.route_owner END,
                        reason = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.reason ELSE excluded.reason END,
                        actor = CASE WHEN thread_policies.policy = 'MUTED_THREAD'
                            THEN thread_policies.actor ELSE excluded.actor END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        row["mailbox"], row["conversation_id"], decision.draft_owner,
                        actor, now, now,
                    ),
                )
            self._event(conn, mail_item_id, "ROUTER_DECISION", row["state"], target.value, actor, {
                "outcome": decision.outcome,
                "owner": decision.draft_owner,
                "watchers": list(decision.watchers),
                "confidence": decision.confidence,
                "reasons": list(decision.reasons),
                "importance": decision.importance.value,
                "urgency": decision.urgency.value,
                "triage_action": decision.triage_action,
                "triage_model": decision.triage_model,
                "triage_confidence": decision.triage_confidence,
                "triage_rationale": decision.triage_rationale,
            })
            return dict(self._get_row(conn, mail_item_id))

    def transition(
        self,
        mail_item_id: str,
        to_state: MailState,
        *,
        actor: str,
        expected_states: Sequence[MailState] | None = None,
        expected_version: int | None = None,
        metadata: dict[str, Any] | None = None,
        patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        patch = patch or {}
        allowed_patch = {
            "deferred_until", "outlook_draft_id", "approval_fingerprint", "last_error",
            "proposal_json", "card_channel", "card_account_id", "card_chat_id", "card_thread_id",
            "card_message_id",
            "deferred_from_state", "send_accepted_at",
            "sent_message_id",
            "replied_sent_id", "replied_sent_at",
            "disposition",
        }
        if set(patch) - allowed_patch:
            raise ValueError(f"Unsupported patch keys: {sorted(set(patch) - allowed_patch)}")
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            current = MailState(row["state"])
            if expected_states is not None and current not in set(expected_states):
                raise ConcurrentUpdate(f"{mail_item_id}: expected {expected_states}, found {current}")
            if expected_version is not None and row["version"] != expected_version:
                raise ConcurrentUpdate(
                    f"{mail_item_id}: expected version {expected_version}, "
                    f"found {row['version']}"
                )
            self._assert_transition(current, to_state)
            assignments = ["state = ?", "updated_at = ?", "version = version + 1"]
            values: list[Any] = [to_state.value, utcnow()]
            for key, value in patch.items():
                assignments.append(f"{key} = ?")
                values.append(value)
            values.extend([mail_item_id, row["version"]])
            result = conn.execute(
                f"UPDATE mail_items SET {', '.join(assignments)} WHERE mail_item_id = ? AND version = ?",  # noqa: S608
                values,
            )
            if result.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            self._event(conn, mail_item_id, "STATE_TRANSITION", current.value, to_state.value, actor, metadata or {})
            return dict(self._get_row(conn, mail_item_id))

    def deny_message(self, mail_item_id: str, *, actor: str = "operator") -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            current = MailState(row["state"])
            self._assert_transition(current, MailState.DENIED_MESSAGE)
            result = conn.execute(
                """UPDATE mail_items SET state = ?, denied_content_hash = content_hash,
                   updated_at = ?, version = version + 1
                   WHERE mail_item_id = ? AND version = ?""",
                (MailState.DENIED_MESSAGE.value, utcnow(), mail_item_id, row["version"]),
            )
            if result.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            self._event(conn, mail_item_id, "MESSAGE_DENIED", current.value, MailState.DENIED_MESSAGE.value, actor, {})
            return dict(self._get_row(conn, mail_item_id))

    def mute_thread(self, mail_item_id: str, *, actor: str = "operator", reason: str | None = None) -> None:
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            if not row["conversation_id"]:
                raise ValueError("Cannot mute a message without a conversation_id")
            now = utcnow()
            conn.execute(
                """
                INSERT INTO thread_policies
                    (mailbox, conversation_id, policy, route_owner, reason, actor, created_at, updated_at)
                VALUES (?, ?, 'MUTED_THREAD', NULL, ?, ?, ?, ?)
                ON CONFLICT(mailbox, conversation_id) DO UPDATE SET
                    policy = excluded.policy, route_owner = NULL, reason = excluded.reason,
                    actor = excluded.actor,
                    updated_at = excluded.updated_at
                """,
                (row["mailbox"], row["conversation_id"], reason, actor, now, now),
            )
            self._event(conn, mail_item_id, "THREAD_MUTED", row["state"], row["state"], actor, {"reason": reason})

    def unmute_thread(self, mailbox: str, conversation_id: str, *, actor: str = "operator") -> bool:
        with self.transaction() as conn:
            result = conn.execute(
                "DELETE FROM thread_policies WHERE mailbox = ? AND conversation_id = ?",
                (mailbox, conversation_id),
            )
            return result.rowcount == 1

    def is_thread_muted(self, mailbox: str, conversation_id: str | None) -> bool:
        if not conversation_id:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM thread_policies WHERE mailbox = ? AND conversation_id = ? AND policy = 'MUTED_THREAD'",
                (mailbox, conversation_id),
            ).fetchone()
            return row is not None

    def set_thread_route(
        self,
        mail_item_id: str,
        owner: str,
        *,
        actor: str = "operator",
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Assign a review item and persist deterministic affinity for its thread."""
        if not owner or not owner.replace("-", "").replace("_", "").isalnum():
            raise ValueError("Invalid thread route owner")
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            if not row["conversation_id"]:
                raise ValueError("Cannot route a thread without a conversation_id")
            if MailState(row["state"]) != MailState.ROUTING_REVIEW:
                raise ConcurrentUpdate(f"{mail_item_id}: no longer awaiting routing review")
            now = utcnow()
            conn.execute(
                """
                INSERT INTO thread_policies
                    (mailbox, conversation_id, policy, route_owner, reason, actor, created_at, updated_at)
                VALUES (?, ?, 'ROUTE_OWNER', ?, ?, ?, ?, ?)
                ON CONFLICT(mailbox, conversation_id) DO UPDATE SET
                    policy = excluded.policy, route_owner = excluded.route_owner,
                    reason = excluded.reason, actor = excluded.actor, updated_at = excluded.updated_at
                """,
                (row["mailbox"], row["conversation_id"], owner, reason, actor, now, now),
            )
            updated = conn.execute(
                """UPDATE mail_items SET state = ?, disposition = ?, draft_owner = ?, route_confidence = 1.0,
                          route_reasons_json = ?, card_message_id = NULL, updated_at = ?,
                          version = version + 1
                   WHERE mail_item_id = ? AND version = ?""",
                (
                    MailState.ROUTED.value, Disposition.REPLY_REQUIRED.value, owner,
                    _json(["THREAD_ASSIGNED_BY_OPERATOR"]),
                    now, mail_item_id, row["version"],
                ),
            )
            if updated.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            self._event(
                conn, mail_item_id, "THREAD_ROUTE_ASSIGNED", row["state"],
                MailState.ROUTED.value, actor, {"owner": owner, "reason": reason},
            )
            return dict(self._get_row(conn, mail_item_id))

    def get_thread_route(self, mailbox: str, conversation_id: str | None) -> str | None:
        if not conversation_id:
            return None
        with self.connect() as conn:
            row = conn.execute(
                """SELECT route_owner FROM thread_policies
                   WHERE mailbox = ? AND conversation_id = ? AND policy = 'ROUTE_OWNER'""",
                (mailbox, conversation_id),
            ).fetchone()
            return row["route_owner"] if row and row["route_owner"] else None

    def record_provider_removal(
        self,
        mailbox: str,
        provider_message_id: str,
        *,
        reason: str | None = None,
        actor: str = "intake",
    ) -> bool:
        """Audit a provider delta removal without inferring deletion or changing state."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM mail_items WHERE mailbox = ? AND provider_message_id = ?",
                (mailbox, provider_message_id),
            ).fetchone()
            if row is None:
                return False
            self._event(
                conn, row["mail_item_id"], "PROVIDER_REMOVAL", row["state"], row["state"],
                actor, {"reason": reason},
            )
            return True

    def defer(self, mail_item_id: str, until: str, *, actor: str = "operator") -> dict[str, Any]:
        item = self.get(mail_item_id)
        if item is None:
            raise KeyError(mail_item_id)
        return self.transition(
            mail_item_id, MailState.DEFERRED, actor=actor,
            patch={"deferred_until": until, "deferred_from_state": item["state"]},
            metadata={"until": until, "from_state": item["state"]},
        )

    def release_due_deferred(
        self, now: str | None = None, *, mailbox: str | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically return due deferrals to their prior approval/drafting stage."""
        released: list[dict[str, Any]] = []
        for item in self.due_deferred(now, mailbox=mailbox):
            previous = item.get("deferred_from_state") or MailState.DRAFT_PROPOSED.value
            try:
                target = MailState(previous)
            except ValueError:
                target = MailState.DRAFT_PROPOSED
            if target not in {
                MailState.ROUTED, MailState.DRAFT_PROPOSED, MailState.SEND_APPROVAL_PENDING,
            }:
                target = MailState.DRAFT_PROPOSED
            try:
                released.append(self.transition(
                    item["mail_item_id"], target, actor="defer-sweeper",
                    expected_states=[MailState.DEFERRED],
                    patch={
                        "deferred_until": None, "deferred_from_state": None,
                        "card_message_id": None,
                    },
                    metadata={"deferred_from_state": previous},
                ))
            except ConcurrentUpdate:
                # A callback may resolve/cancel the item after the due query.
                continue
        return released

    def request_draft(self, mail_item_id: str, *, actor: str = "dispatcher") -> dict[str, Any]:
        return self.transition(
            mail_item_id, MailState.DRAFT_REQUESTED, actor=actor,
            expected_states=[MailState.ROUTED],
        )

    def start_drafting(
        self, mail_item_id: str, *, actor: str = "dispatcher",
        expected_states: Sequence[MailState] = (MailState.DRAFT_REQUESTED,),
    ) -> dict[str, Any]:
        """Atomically lease one draft to one worker by moving it out of the queue."""
        return self.transition(
            mail_item_id, MailState.DRAFTING, actor=actor,
            expected_states=expected_states,
        )

    def record_conversation(
        self, mail_item_id: str, messages: list[dict[str, Any]], *, actor: str = "context-reader",
    ) -> dict[str, Any]:
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            result = conn.execute(
                """UPDATE mail_items SET conversation_messages_json = ?, updated_at = ?,
                   version = version + 1 WHERE mail_item_id = ? AND version = ?""",
                (_json(messages), utcnow(), mail_item_id, row["version"]),
            )
            if result.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            self._event(
                conn, mail_item_id, "CONVERSATION_CONTEXT_REFRESHED", row["state"], row["state"],
                actor, {"message_count": len(messages)},
            )
            return dict(self._get_row(conn, mail_item_id))

    def propose_draft(
        self,
        mail_item_id: str,
        proposal: dict[str, Any],
        *,
        actor: str = "draft-agent",
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        return self.transition(
            mail_item_id, MailState.DRAFT_PROPOSED, actor=actor,
            expected_states=[MailState.DRAFTING],
            expected_version=expected_version,
            patch={"proposal_json": _json(proposal), "last_error": None},
            metadata={
                "proposal_keys": sorted(proposal),
                "draft_provenance": proposal.get("_mailroom_provenance"),
            },
        )

    def attach_card(
        self,
        mail_item_id: str,
        *,
        channel: str,
        account_id: str,
        chat_id: str,
        message_id: str,
        thread_id: str | None = None,
        actor: str = "notifier",
    ) -> dict[str, Any]:
        thread_id = str(thread_id) if thread_id else None
        with self.transaction() as conn:
            row = self._get_row(conn, mail_item_id)
            result = conn.execute(
                """
                UPDATE mail_items SET card_channel = ?, card_account_id = ?, card_chat_id = ?,
                    card_thread_id = ?, card_message_id = ?, updated_at = ?, version = version + 1
                WHERE mail_item_id = ? AND version = ?
                """,
                (
                    channel, account_id, chat_id, thread_id, message_id, utcnow(),
                    mail_item_id, row["version"],
                ),
            )
            if result.rowcount != 1:
                raise ConcurrentUpdate(mail_item_id)
            self._event(
                conn, mail_item_id, "APPROVAL_CARD_ATTACHED", row["state"], row["state"], actor,
                {
                    "channel": channel, "account_id": account_id, "chat_id": chat_id,
                    "thread_id": thread_id, "message_id": message_id,
                },
            )
            return dict(self._get_row(conn, mail_item_id))

    def get_callback_item(
        self,
        callback_token: str,
        *,
        channel: str,
        account_id: str,
        chat_id: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM mail_items
                WHERE callback_token = ? AND card_channel = ? AND card_account_id = ?
                  AND card_chat_id = ? AND card_message_id = ?
                """,
                (callback_token, channel, account_id, chat_id, message_id),
            ).fetchone()
            return dict(row) if row else None

    def due_deferred(
        self, now: str | None = None, *, mailbox: str | None = None,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            mailbox_clause = " AND mailbox = ?" if mailbox else ""
            values: list[Any] = [MailState.DEFERRED.value, now or utcnow()]
            if mailbox:
                values.append(mailbox)
            rows = conn.execute(
                """SELECT * FROM mail_items WHERE state = ? AND deferred_until IS NOT NULL
                   AND deferred_until <= ?""" + mailbox_clause + " ORDER BY deferred_until",
                values,
            ).fetchall()
            return [dict(r) for r in rows]

    def create_pending_input(
        self,
        mail_item_id: str,
        *,
        kind: str,
        channel: str,
        chat_id: str,
        card_message_id: str,
        expected_sender_id: str,
        expires_at: str,
        account_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a fail-closed correlation record for a future free-text reply."""
        pending_id = str(uuid.uuid4())
        now = utcnow()
        with self.transaction() as conn:
            self._get_row(conn, mail_item_id)
            conn.execute(
                """
                INSERT INTO pending_inputs (
                    pending_input_id, mail_item_id, kind, channel, account_id, chat_id,
                    card_message_id, expected_sender_id, expires_at, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending_id, mail_item_id, kind, channel, account_id, chat_id,
                    card_message_id, expected_sender_id, expires_at, _json(payload or {}), now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM pending_inputs WHERE pending_input_id = ?", (pending_id,)
            ).fetchone()
            return dict(row)

    def resolve_pending_input(
        self,
        *,
        channel: str,
        chat_id: str,
        card_message_id: str,
        sender_id: str,
        account_id: str | None,
        now: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically consume a matching, unexpired prompt; ambiguity fails closed."""
        resolved_at = now or utcnow()
        with self.transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM pending_inputs
                WHERE channel = ? AND chat_id = ? AND card_message_id = ?
                  AND account_id IS ?
                  AND expected_sender_id = ? AND resolved_at IS NULL AND expires_at > ?
                ORDER BY created_at DESC
                """,
                (channel, chat_id, card_message_id, account_id, sender_id, resolved_at),
            ).fetchall()
            if len(rows) != 1:
                return None
            row = rows[0]
            updated = conn.execute(
                """UPDATE pending_inputs SET resolved_at = ?
                   WHERE pending_input_id = ? AND resolved_at IS NULL""",
                (resolved_at, row["pending_input_id"]),
            )
            if updated.rowcount != 1:
                return None
            result = dict(row)
            result["resolved_at"] = resolved_at
            return result

    def get(self, mail_item_id_or_token: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM mail_items WHERE mail_item_id = ? OR callback_token = ?",
                (mail_item_id_or_token, mail_item_id_or_token),
            ).fetchone()
            return dict(row) if row else None

    def list_items(
        self,
        *,
        state: MailState | None = None,
        run_mode: str | None = None,
        mailbox: str | None = None,
        card_attached: bool | None = None,
        limit: int = 50,
        order_by_priority: bool = False,
    ) -> list[dict[str, Any]]:
        with self.connect() as conn:
            clauses = []
            values: list[Any] = []
            if state:
                clauses.append("state = ?")
                values.append(state.value)
            if run_mode:
                if run_mode not in {"shadow", "production"}:
                    raise ValueError(f"Unsupported run_mode: {run_mode}")
                clauses.append("run_mode = ?")
                values.append(run_mode)
            if mailbox:
                clauses.append("mailbox = ?")
                values.append(mailbox)
            if card_attached is True:
                clauses.append("card_message_id IS NOT NULL")
            elif card_attached is False:
                clauses.append("card_message_id IS NULL")
            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            values.append(limit)
            order = (
                "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 "
                "WHEN 'P2' THEN 2 ELSE 3 END, updated_at ASC"
                if order_by_priority else "updated_at DESC"
            )
            rows = conn.execute(
                f"SELECT * FROM mail_items{where} ORDER BY {order} LIMIT ?",  # noqa: S608
                values,
            ).fetchall()
            return [dict(r) for r in rows]

    def get_checkpoint(self, adapter: str, mailbox: str, scope: str) -> str | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT cursor FROM intake_checkpoints WHERE adapter = ? AND mailbox = ? AND scope = ?",
                (adapter, mailbox, scope),
            ).fetchone()
            return row["cursor"] if row else None

    def save_checkpoint(
        self,
        adapter: str,
        mailbox: str,
        scope: str,
        cursor: str | None,
        *,
        error: str | None = None,
        expected_cursor: str | None | object = _UNSET,
    ) -> None:
        now = utcnow()
        with self.transaction() as conn:
            current = conn.execute(
                "SELECT cursor FROM intake_checkpoints WHERE adapter = ? AND mailbox = ? AND scope = ?",
                (adapter, mailbox, scope),
            ).fetchone()
            if expected_cursor is not _UNSET:
                current_cursor = current["cursor"] if current else None
                if current_cursor == cursor:
                    return
                if current_cursor != expected_cursor:
                    raise ConcurrentUpdate(
                        f"Checkpoint changed for {adapter}/{mailbox}/{scope}"
                    )
            conn.execute(
                """
                INSERT INTO intake_checkpoints (adapter, mailbox, scope, cursor, last_success_at, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(adapter, mailbox, scope) DO UPDATE SET
                    cursor = excluded.cursor,
                    last_success_at = excluded.last_success_at,
                    last_error = excluded.last_error,
                    updated_at = excluded.updated_at
                """,
                (adapter, mailbox, scope, cursor, None if error else now, error, now),
            )

    def _get_row(self, conn: sqlite3.Connection, mail_item_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM mail_items WHERE mail_item_id = ?", (mail_item_id,)).fetchone()
        if row is None:
            raise KeyError(mail_item_id)
        return row

    @staticmethod
    def _assert_transition(current: MailState, target: MailState) -> None:
        if target not in ALLOWED_TRANSITIONS.get(current, set()):
            raise InvalidTransition(f"{current.value} -> {target.value}")

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        mail_item_id: str,
        event_type: str,
        from_state: str | None,
        to_state: str | None,
        actor: str,
        metadata: dict[str, Any],
    ) -> None:
        conn.execute(
            """INSERT INTO mail_events
               (event_id, mail_item_id, event_type, from_state, to_state, actor, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), mail_item_id, event_type, from_state, to_state, actor, _json(metadata), utcnow()),
        )
