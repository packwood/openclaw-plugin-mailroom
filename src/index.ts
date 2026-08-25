import { createHash, randomUUID } from "node:crypto";
import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { basename, join } from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";
import { promisify } from "node:util";
import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import {
  checkOutlookThreadUpdates,
  configureOutlookAdapter,
  createOutlookReplyDraft,
  deleteOutlookDraft,
  sendOutlookDraft,
} from "./outlook.js";
import {
  DEFAULT_PROFILES_PATH,
  publishRoutingOwnerPolicy,
  resolveRoutingOwnerIds,
  type RoutingOwnerMode,
} from "./routing-owners.js";

const execFileAsync = promisify(execFile);

export type TelegramDestination = {
  chatId: string;
  threadId?: string;
};

type Config = {
  dbPath: string;
  pythonPath: string;
  pythonExecutable?: string;
  telegramChatId: string;
  telegramThreadId?: string;
  telegramDestinations?: Record<string, TelegramDestination>;
  revisionApprovers?: string[];
  routingReviewAgentId: string;
  routingReviewTelegramAccountId: string;
  reviewOwners: string[];
  routingOwnerMode?: RoutingOwnerMode;
  profilesPath?: string;
  agentDiscovery?: () => Promise<string[]>;
  accounts?: Record<string, string>;
  connectionsPath?: string;
  signaturesPath?: string;
  revisionRunner?: (
    token: string, instructions: string, accountId: string, chatId: string,
  ) => Promise<Record<string, any>>;
  policyValidator?: (item: Item) => Promise<Record<string, any>>;
  draftCreator?: typeof createOutlookReplyDraft;
  draftDeleter?: typeof deleteOutlookDraft;
  draftSender?: typeof sendOutlookDraft;
  threadChecker?: typeof checkOutlookThreadUpdates;
  routeVerifier?: (
    db: DatabaseSync, itemId: string, mailbox: string,
    conversationId: string, owner: string,
  ) => boolean;
};

type Item = Record<string, any>;

const ROUTE_VERIFIED_STATES = new Set([
  "ROUTED",
  "DRAFT_REQUESTED",
  "DRAFT_PROPOSED",
]);

export function sendFailureState(result: Record<string, any>): "SEND_APPROVAL_PENDING" | "SEND_OUTCOME_UNKNOWN" {
  return result.send_attempted === true ? "SEND_OUTCOME_UNKNOWN" : "SEND_APPROVAL_PENDING";
}

const LOCAL_ERROR_NOTE = "Details were recorded in local Mailroom logs.";

/** Record internal diagnostics locally without copying them into Telegram. */
function logInternalError(context: string, error: unknown): void {
  console.error(`[mailroom] ${context}`, error);
}

export function revisionFailureMessage(_error: any): string {
  return `Revision failed safely; nothing was sent. ${LOCAL_ERROR_NOTE}`;
}

function revisionResultMessage(result: Record<string, any>): string {
  if (result.suppressed === true) {
    return "✅ Revision suppressed because a newer manual reply exists in Sent Items.";
  }
  if (result.ok === true) {
    return "✅ Revised proposal drafted. A new approval card was sent.";
  }
  if (result.error) logInternalError("revision command failed", result.error);
  return `Revision failed safely; nothing was sent. ${LOCAL_ERROR_NOTE}`;
}

export function invalidInstructionsMessage(instructions: string): string | null {
  if (!instructions) return "Revision instructions were empty. Nothing was changed.";
  if (instructions.length > 4096) {
    return "Revision instructions are too long. Nothing was changed; please send a shorter reply.";
  }
  // A leading "-" could be parsed as a CLI flag by the Python revision command.
  if (instructions.startsWith("-")) {
    return 'Revision instructions may not begin with "-". Nothing was changed; please rephrase.';
  }
  return null;
}

const TOPIC_CONVERSATION = /^(.*):topic:(\d+)$/;

export function parseTelegramConversationId(
  conversationId: string,
): { chatId: string; threadId?: string } {
  const value = String(conversationId || "");
  const match = value.match(TOPIC_CONVERSATION);
  if (match) return { chatId: match[1], threadId: match[2] };
  return { chatId: value };
}

function isTelegramGroupChat(chatId: string): boolean {
  return String(chatId).startsWith("-");
}

function groupRevisionAuthorized(ctx: Record<string, any>): boolean {
  return ctx.auth?.isAuthorizedSender === true || ctx.isAuthorizedSender === true;
}

function parseRevisionApprovers(raw: unknown, telegramChatId: string): string[] {
  if (raw == null) {
    return telegramChatId && !isTelegramGroupChat(telegramChatId) ? [telegramChatId] : [];
  }
  if (!Array.isArray(raw)) {
    throw new Error("Mailroom revisionApprovers must be an array of non-empty strings");
  }
  return raw.map((value, index) => {
    const id = String(value ?? "").trim();
    if (!id) {
      throw new Error(`Mailroom revisionApprovers[${index}] must be a non-empty string`);
    }
    return id;
  });
}

function resolvedRevisionApprovers(cfg: Config): string[] {
  if (cfg.revisionApprovers !== undefined) return cfg.revisionApprovers;
  return parseRevisionApprovers(undefined, String(cfg.telegramChatId || ""));
}

function groupRevisionReplyAuthorized(
  ctx: Record<string, any>, senderId: string, approvers: string[],
): boolean {
  if (groupRevisionAuthorized(ctx)) return true;
  return approvers.includes(senderId);
}

export function resolveTelegramDestination(
  destinations: Record<string, TelegramDestination> | undefined,
  owner: string | null | undefined,
  fallback: TelegramDestination,
  reviewAgentId: string,
): TelegramDestination {
  const key = String(owner || "").trim() || reviewAgentId;
  const match = key ? destinations?.[key] : undefined;
  if (match?.chatId) return { chatId: match.chatId, threadId: match.threadId };
  return { chatId: fallback.chatId, threadId: fallback.threadId };
}

function parseTelegramDestinations(raw: unknown): Record<string, TelegramDestination> {
  if (raw == null) return {};
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new Error(
      "Mailroom telegramDestinations must be an object mapping OpenClaw agent ids to destinations",
    );
  }
  const destinations: Record<string, TelegramDestination> = {};
  for (const [agentId, value] of Object.entries(raw as Record<string, unknown>)) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new Error(
        `Mailroom telegramDestinations.${agentId} must be an object with a non-empty chatId`,
      );
    }
    const entry = value as Record<string, unknown>;
    const chatId = String(entry.chatId ?? "").trim();
    if (!chatId) {
      throw new Error(
        `Mailroom telegramDestinations.${agentId} requires a non-empty chatId`,
      );
    }
    const destination: TelegramDestination = { chatId };
    if (entry.threadId != null) {
      const threadId = String(entry.threadId).trim();
      if (!threadId) {
        throw new Error(
          `Mailroom telegramDestinations.${agentId} threadId must be a non-empty string when set`,
        );
      }
      destination.threadId = threadId;
    }
    destinations[String(agentId)] = destination;
  }
  return destinations;
}

export function resolveConfig(raw: any): Config {
  const telegramChatId = String(
    raw?.telegramChatId || process.env.MAILROOM_TELEGRAM_CHAT_ID || "",
  );
  const telegramThreadId = raw?.telegramThreadId ? String(raw.telegramThreadId) : undefined;
  const telegramDestinations = parseTelegramDestinations(raw?.telegramDestinations);
  const revisionApprovers = parseRevisionApprovers(raw?.revisionApprovers, telegramChatId);
  const routingOwnerMode = raw?.routingOwnerMode === undefined
    ? "all"
    : String(raw.routingOwnerMode);
  if (routingOwnerMode !== "all" && routingOwnerMode !== "selected") {
    throw new Error("Mailroom routingOwnerMode must be 'all' or 'selected'");
  }
  const reviewOwners = Array.isArray(raw?.reviewOwners)
    ? raw.reviewOwners.map(String)
    : [];
  if (routingOwnerMode === "selected" && reviewOwners.length === 0) {
    throw new Error("Mailroom selected routing-owner mode requires reviewOwners");
  }
  const profilesPath = raw?.profilesPath
    ? String(raw.profilesPath)
    : DEFAULT_PROFILES_PATH;
  if (basename(profilesPath) !== "current.json") {
    throw new Error("Mailroom profilesPath must end with current.json");
  }
  return {
    dbPath: raw?.dbPath || join(homedir(), ".openclaw", "mailroom", "mailroom.db"),
    pythonPath: raw?.pythonPath || fileURLToPath(new URL("../python", import.meta.url)),
    pythonExecutable: String(raw?.pythonExecutable || "python3"),
    telegramChatId,
    telegramThreadId,
    telegramDestinations,
    revisionApprovers,
    routingReviewAgentId: String(raw?.routingReviewAgentId || "main"),
    routingReviewTelegramAccountId: String(
      raw?.routingReviewTelegramAccountId || "default",
    ),
    reviewOwners,
    routingOwnerMode,
    profilesPath,
    accounts: raw?.accounts && typeof raw.accounts === "object"
      ? Object.fromEntries(Object.entries(raw.accounts).map(([key, value]) => [key, String(value)]))
      : {},
    connectionsPath: raw?.connectionsPath ? String(raw.connectionsPath) : undefined,
    signaturesPath: raw?.signaturesPath ? String(raw.signaturesPath) : undefined,
  };
}

function openDb(path: string): DatabaseSync {
  const db = new DatabaseSync(path);
  db.exec("PRAGMA foreign_keys = ON");
  db.exec("PRAGMA busy_timeout = 30000");
  db.exec(`
    CREATE TABLE IF NOT EXISTS adopted_provider_messages (
      mailbox TEXT NOT NULL,
      provider_message_id TEXT NOT NULL,
      mail_item_id TEXT NOT NULL REFERENCES mail_items(mail_item_id),
      adopted_at TEXT NOT NULL,
      PRIMARY KEY(mailbox, provider_message_id)
    );
    CREATE INDEX IF NOT EXISTS ix_adopted_mail_item
      ON adopted_provider_messages(mail_item_id);
  `);
  return db;
}

function event(
  db: DatabaseSync, itemId: string, type: string, fromState: string,
  toState: string, actor: string, metadata: Record<string, any> = {},
): void {
  db.prepare(`
    INSERT INTO mail_events
      (event_id, mail_item_id, event_type, from_state, to_state, actor, metadata_json, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
  `).run(
    randomUUID(), itemId, type, fromState, toState, actor,
    JSON.stringify(metadata), new Date().toISOString(),
  );
}

function callbackItem(db: DatabaseSync, ctx: any, token: string): Item | null {
  return (db.prepare(`
    SELECT * FROM mail_items
    WHERE callback_token = ? AND run_mode = 'production'
      AND card_channel = 'telegram' AND card_account_id = ?
      AND card_chat_id = ? AND card_message_id = ?
  `).get(
    token, String(ctx.accountId), String(ctx.callback.chatId), String(ctx.callback.messageId),
  ) as Item | undefined) ?? null;
}

const CLAIM_PATCH_COLUMNS = new Set([
  "approval_fingerprint", "card_message_id", "denied_content_hash",
  "deferred_from_state", "deferred_until", "last_error",
  "new_email_checked_at", "outlook_draft_id", "replied_sent_at",
  "replied_sent_id", "send_accepted_at",
]);

function claim(
  db: DatabaseSync, item: Item, expected: string[], target: string,
  actor: string, patch: Record<string, any> = {},
): Item | null {
  if (!expected.includes(item.state)) return null;
  const keys = Object.keys(patch);
  const assignments = ["state = ?", "updated_at = ?", "version = version + 1"];
  const values: any[] = [target, new Date().toISOString()];
  for (const key of keys) {
    if (!CLAIM_PATCH_COLUMNS.has(key)) {
      throw new Error(`Unexpected mail_items patch column: ${key}`);
    }
    assignments.push(`${key} = ?`);
    values.push(patch[key]);
  }
  values.push(item.mail_item_id, item.version);
  db.exec("BEGIN IMMEDIATE");
  try {
    const result = db.prepare(
      `UPDATE mail_items SET ${assignments.join(", ")} WHERE mail_item_id = ? AND version = ?`,
    ).run(...values);
    if (result.changes !== 1) {
      db.exec("ROLLBACK");
      return null;
    }
    event(db, item.mail_item_id, "INTERACTIVE_TRANSITION", item.state, target, actor);
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return db.prepare("SELECT * FROM mail_items WHERE mail_item_id = ?").get(item.mail_item_id) as Item;
}

const ADOPTABLE_DUPLICATE_STATES = new Set([
  "INGESTED", "ROUTING_REVIEW", "ROUTED", "DRAFT_REQUESTED",
  "DRAFT_PROPOSED", "REVISION_REQUESTED", "DEFERRED", "DROPPED",
]);

function adoptRelatedMessagesAndRequeue(
  db: DatabaseSync,
  item: Item,
  latest: Record<string, any>,
  relatedMessages: Record<string, any>[],
): Item | null {
  const now = new Date().toISOString();
  db.exec("BEGIN IMMEDIATE");
  try {
    const current = db.prepare(
      "SELECT * FROM mail_items WHERE mail_item_id = ?",
    ).get(item.mail_item_id) as Item | undefined;
    if (!current || current.state !== "REVISION_REQUESTED" || current.version !== item.version) {
      db.exec("ROLLBACK");
      return null;
    }

    for (const message of relatedMessages) {
      const providerMessageId = String(message?.message_id || "");
      if (!providerMessageId) throw new Error("Newer Inbox message is missing its provider id");
      const duplicate = db.prepare(`
        SELECT * FROM mail_items
        WHERE mailbox = ? AND provider_message_id = ? AND mail_item_id != ?
      `).get(item.mailbox, providerMessageId, item.mail_item_id) as Item | undefined;
      if (duplicate) {
        if (duplicate.outlook_draft_id || !ADOPTABLE_DUPLICATE_STATES.has(duplicate.state)) {
          throw new Error("A newer Inbox message already has an active Mailroom workflow");
        }
        const dropped = db.prepare(`
          UPDATE mail_items SET state = 'DROPPED', card_message_id = NULL,
            last_error = NULL, updated_at = ?, version = version + 1
          WHERE mail_item_id = ? AND version = ?
        `).run(now, duplicate.mail_item_id, duplicate.version);
        if (dropped.changes !== 1) {
          throw new Error("A newer Inbox item changed while it was being consolidated");
        }
        event(
          db, duplicate.mail_item_id, "SUPERSEDED_BY_THREAD_REFRESH",
          duplicate.state, "DROPPED", "operator:new-email-check",
          { canonical_mail_item_id: item.mail_item_id },
        );
      }

      const existingAlias = db.prepare(`
        SELECT mail_item_id FROM adopted_provider_messages
        WHERE mailbox = ? AND provider_message_id = ?
      `).get(item.mailbox, providerMessageId) as { mail_item_id: string } | undefined;
      if (existingAlias && existingAlias.mail_item_id !== item.mail_item_id) {
        throw new Error("A newer Inbox message is already attached to another Mailroom item");
      }
      db.prepare(`
        INSERT OR IGNORE INTO adopted_provider_messages
          (mailbox, provider_message_id, mail_item_id, adopted_at)
        VALUES (?, ?, ?, ?)
      `).run(item.mailbox, providerMessageId, item.mail_item_id, now);
    }

    const updated = db.prepare(`
      UPDATE mail_items SET state = 'DRAFT_REQUESTED',
        reply_target_message_id = ?, reply_target_received_at = ?,
        reply_target_sender_email = ?, related_messages_json = ?,
        new_email_checked_at = ?, outlook_draft_id = NULL,
        approval_fingerprint = NULL, card_message_id = NULL,
        last_error = NULL, updated_at = ?, version = version + 1
      WHERE mail_item_id = ? AND state = 'REVISION_REQUESTED' AND version = ?
    `).run(
      latest.message_id, latest.received_at, latest.sender_email,
      JSON.stringify(relatedMessages), now, now,
      item.mail_item_id, item.version,
    );
    if (updated.changes !== 1) {
      db.exec("ROLLBACK");
      return null;
    }
    event(
      db, item.mail_item_id, "INTERACTIVE_TRANSITION",
      "REVISION_REQUESTED", "DRAFT_REQUESTED", "operator:new-email-check",
      { adopted_provider_message_ids: relatedMessages.map((message) => message.message_id) },
    );
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return db.prepare("SELECT * FROM mail_items WHERE mail_item_id = ?").get(item.mail_item_id) as Item;
}

function finishDraft(db: DatabaseSync, item: Item, result: Record<string, any>): Item {
  db.exec("BEGIN IMMEDIATE");
  try {
    const updated = db.prepare(`
      UPDATE mail_items SET state = 'SEND_APPROVAL_PENDING', outlook_draft_id = ?,
        approval_fingerprint = ?, card_message_id = NULL, last_error = NULL,
        updated_at = ?, version = version + 1
      WHERE mail_item_id = ? AND state = 'OUTLOOK_DRAFTING' AND version = ?
    `).run(
      result.draft_id, result.approval_token, new Date().toISOString(),
      item.mail_item_id, item.version,
    );
    if (updated.changes !== 1) throw new Error("Draft completion lost its state claim");
    event(db, item.mail_item_id, "OUTLOOK_DRAFT_CREATED", "OUTLOOK_DRAFTING", "OUTLOOK_DRAFTED", "mailroom-plugin", {
      draft_id: result.draft_id,
    });
    event(db, item.mail_item_id, "SEND_APPROVAL_REQUESTED", "OUTLOOK_DRAFTED", "SEND_APPROVAL_PENDING", "mailroom-plugin");
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return db.prepare("SELECT * FROM mail_items WHERE mail_item_id = ?").get(item.mail_item_id) as Item;
}

function failState(db: DatabaseSync, itemId: string, expected: string, target: string, error: string): void {
  db.exec("BEGIN IMMEDIATE");
  try {
    const current = db.prepare("SELECT * FROM mail_items WHERE mail_item_id = ?").get(itemId) as Item | undefined;
    if (!current) {
      db.exec("ROLLBACK");
      return;
    }
    const updated = db.prepare(`
      UPDATE mail_items SET state = ?, last_error = ?, updated_at = ?, version = version + 1
      WHERE mail_item_id = ? AND state = ? AND version = ?
    `).run(target, error.slice(0, 2000), new Date().toISOString(), itemId, expected, current.version);
    if (updated.changes === 1) event(db, itemId, "INTERACTIVE_ERROR", expected, target, "mailroom-plugin", { error: error.slice(0, 500) });
    db.exec("COMMIT");
  } catch (failure) {
    db.exec("ROLLBACK");
    throw failure;
  }
}

function markRepliedElsewhere(
  db: DatabaseSync, item: Item, expected: string, result: Record<string, any>,
): Item | null {
  const now = new Date().toISOString();
  db.exec("BEGIN IMMEDIATE");
  try {
    const updated = db.prepare(`
      UPDATE mail_items SET state = 'REPLIED_ELSEWHERE', replied_sent_id = ?,
        replied_sent_at = ?, last_error = NULL, updated_at = ?, version = version + 1
      WHERE mail_item_id = ? AND state = ? AND version = ?
    `).run(
      result.message_id || null, result.sent_at || null, now,
      item.mail_item_id, expected, item.version,
    );
    if (updated.changes !== 1) {
      db.exec("ROLLBACK");
      return null;
    }
    event(db, item.mail_item_id, "MANUAL_REPLY_DETECTED", expected, "REPLIED_ELSEWHERE", "sent-items-guard", {
      sent_message_id: result.message_id || null, sent_at: result.sent_at || null,
    });
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return db.prepare("SELECT * FROM mail_items WHERE mail_item_id = ?").get(item.mail_item_id) as Item;
}

function assignThreadRoute(db: DatabaseSync, item: Item, owner: string): Item | null {
  if (item.state !== "ROUTING_REVIEW" || !item.conversation_id) return null;
  const now = new Date().toISOString();
  db.exec("BEGIN IMMEDIATE");
  try {
    db.prepare(`
      INSERT INTO thread_policies
        (mailbox, conversation_id, policy, route_owner, reason, actor, created_at, updated_at)
      VALUES (?, ?, 'ROUTE_OWNER', ?, 'Telegram routing review', 'operator:telegram', ?, ?)
      ON CONFLICT(mailbox, conversation_id) DO UPDATE SET
        policy = excluded.policy, route_owner = excluded.route_owner,
        reason = excluded.reason, actor = excluded.actor, updated_at = excluded.updated_at
    `).run(item.mailbox, item.conversation_id, owner, now, now);
    const updated = db.prepare(`
      UPDATE mail_items SET state = 'ROUTED', disposition = 'reply_required',
        draft_owner = ?, route_confidence = 1.0,
        route_reasons_json = '["THREAD_ASSIGNED_BY_OPERATOR"]', card_message_id = NULL,
        updated_at = ?, version = version + 1
      WHERE mail_item_id = ? AND state = 'ROUTING_REVIEW' AND version = ?
    `).run(owner, now, item.mail_item_id, item.version);
    if (updated.changes !== 1) {
      db.exec("ROLLBACK");
      return null;
    }
    event(db, item.mail_item_id, "THREAD_ROUTE_ASSIGNED", "ROUTING_REVIEW", "ROUTED", "operator:telegram", { owner });
    db.exec("COMMIT");
  } catch (error) {
    db.exec("ROLLBACK");
    throw error;
  }
  return db.prepare("SELECT * FROM mail_items WHERE mail_item_id = ?").get(item.mail_item_id) as Item;
}

export function verifyThreadRoute(
  db: DatabaseSync,
  itemId: string,
  mailbox: string,
  conversationId: string,
  owner: string,
): boolean {
  const verified = db.prepare(`
    SELECT m.state, m.draft_owner,
      p.policy, p.route_owner
    FROM mail_items m
    LEFT JOIN thread_policies p
      ON p.mailbox = m.mailbox AND p.conversation_id = m.conversation_id
    WHERE m.mail_item_id = ? AND m.mailbox = ? AND m.conversation_id = ?
  `).get(itemId, mailbox, conversationId) as Record<string, any> | undefined;
  const assignment = db.prepare(`
    SELECT metadata_json
    FROM mail_events
    WHERE mail_item_id = ? AND event_type = 'THREAD_ROUTE_ASSIGNED'
    ORDER BY created_at DESC, rowid DESC
    LIMIT 1
  `).get(itemId) as { metadata_json?: string } | undefined;
  let assignmentOwner: string | null = null;
  try {
    const metadata = JSON.parse(assignment?.metadata_json || "null");
    assignmentOwner = typeof metadata?.owner === "string" ? metadata.owner : null;
  } catch {
    return false;
  }
  return Boolean(
    verified
      && ROUTE_VERIFIED_STATES.has(verified.state)
      && verified.draft_owner === owner
      && verified.policy === "ROUTE_OWNER"
      && verified.route_owner === owner
      && assignmentOwner === owner
  );
}

function accountForMailbox(cfg: Config, mailbox: string): string {
  const normalized = mailbox.toLowerCase();
  const match = Object.entries(cfg.accounts ?? {}).find(
    ([, address]) => address.toLowerCase() === normalized,
  );
  return match?.[0] ?? mailbox;
}

export function routingOwnerCallbackRef(owner: string): string {
  return createHash("sha256").update(owner, "utf8").digest("hex").slice(0, 16);
}

function proposal(item: Item): Record<string, any> {
  const value = JSON.parse(item.proposal_json || "{}");
  if (typeof value.reply_text !== "string" || !value.reply_text.trim()) {
    throw new Error("Stored proposal has no reply_text");
  }
  return value;
}

async function validateProposalPolicy(cfg: Config, item: Item): Promise<Record<string, any>> {
  if (cfg.policyValidator) return cfg.policyValidator(item);
  const { stdout } = await execFileAsync(
    cfg.pythonExecutable || "python3",
    ["-m", "mailroom.cli", "--db", cfg.dbPath, "validate-proposal", item.mail_item_id],
    {
      timeout: 30000,
      env: { ...process.env, PYTHONPATH: cfg.pythonPath },
      maxBuffer: 1024 * 1024,
    },
  );
  const result = JSON.parse(stdout);
  if (typeof result?.ok !== "boolean" || !Array.isArray(result?.violations)) {
    throw new Error("Mailroom draft validator returned a malformed result");
  }
  return result;
}

function truncate(value: string, limit: number): string {
  const clean = value.trim();
  if (clean.length <= limit) return clean;
  const marker = "\n[…truncated]";
  return clean.slice(0, limit - marker.length).trimEnd() + marker;
}

/**
 * Break revision-token markers and /mr-revise commands that arrive inside
 * untrusted email content with a zero-width space, so embedded text can never
 * be parsed as (or visually spoof) a genuine Mailroom marker.
 */
function neutralizeMarkers(value: string): string {
  return value
    .replace(/(mailroom\s+revision\s+)(token)/gi, "$1\u200B$2")
    .replace(/(^|\n)([>\s]*)\/(mr-revise)/gi, "$1$2/\u200B$3");
}

/** Quote-prefix untrusted email content so it cannot masquerade as card UI text. */
function quoteLines(value: string): string {
  return value.split("\n").map((line) => `> ${line}`).join("\n");
}

function decodeHtml(value: string): string {
  return value
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;|&apos;/gi, "'")
    .replace(/&#(\d+);/g, (match, value) => {
      const codePoint = Number(value);
      return Number.isInteger(codePoint) && codePoint >= 0 && codePoint <= 0x10ffff
        ? String.fromCodePoint(codePoint)
        : match;
    })
    .replace(/&#x([0-9a-f]+);/gi, (match, value) => {
      const codePoint = Number.parseInt(value, 16);
      return Number.isInteger(codePoint) && codePoint >= 0 && codePoint <= 0x10ffff
        ? String.fromCodePoint(codePoint)
        : match;
    });
}

function originalEmailText(item: Item): string {
  let value = String(item.body_content || item.body_preview || "");
  const markers = [
    /<blockquote\b/i,
    /<div[^>]+(?:id|class)=["'][^"']*(?:divRplyFwdMsg|gmail_quote)[^"']*["']/i,
    /-----\s*Original Message\s*-----/i,
    /(?:^|\n)From:\s.*\nSent:\s/is,
  ];
  const positions = markers.map((pattern) => value.search(pattern)).filter((position) => position >= 0);
  if (positions.length) value = value.slice(0, Math.min(...positions));
  value = value
    .replace(/<(?:head|style|script)\b[^>]*>[\s\S]*?<\/(?:head|style|script)>/gi, " ")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/(?:p|div|li)>/gi, "\n")
    .replace(/<[^>]+>/g, " ");
  return decodeHtml(value)
    .replace(/[ \t]+/g, " ")
    .replace(/\s*\n\s*/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function sizeSuffix(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return "";
  if (value < 1024) return ` (${value} B)`;
  if (value < 1024 * 1024) return ` (${(value / 1024).toFixed(1)} KB)`;
  return ` (${(value / (1024 * 1024)).toFixed(1)} MB)`;
}

function attachmentText(item: Item, limit = 500): string {
  if (!item.has_attachments) return "Attachments: none";
  if (typeof item.attachments_json !== "string") return "Attachments: present; filenames unavailable";
  let attachments: any[];
  try {
    attachments = JSON.parse(item.attachments_json);
    if (!Array.isArray(attachments)) throw new Error("not an array");
  } catch {
    return "Attachments: present; filenames unavailable";
  }
  const files = attachments.filter((attachment) => attachment?.is_inline !== true);
  const inlineCount = attachments.length - files.length;
  if (!files.length) return inlineCount ? `Attachments: inline only (${inlineCount})` : "Attachments: none";
  const lines = files.map((attachment) =>
    `- ${truncate(String(attachment?.name || "(unnamed)").replace(/\s+/g, " "), 300).replace(/\n/g, " ")}${sizeSuffix(attachment?.size)}`,
  );
  if (inlineCount) lines.push(`- plus ${inlineCount} inline attachment(s)`);
  return truncate(`Attachments:\n${lines.join("\n")}`, limit);
}

function recipientText(item: Item, limit = 500): string {
  let raw: Record<string, any> = {};
  try {
    raw = JSON.parse(String(item.raw_json || "{}"));
  } catch {
    raw = {};
  }
  const recipients = (key: string): string[] => {
    const values = raw[key];
    if (!Array.isArray(values)) return [];
    return values.flatMap((value: any) => {
      const address = value?.emailAddress;
      const email = String(address?.address || "").trim().replace(/\s+/g, " ");
      const name = String(address?.name || "").trim().replace(/\s+/g, " ");
      if (!email) return [];
      return [name && name.toLowerCase() !== email.toLowerCase() ? `${name} <${email}>` : email];
    });
  };
  const to = recipients("toRecipients");
  const cc = recipients("ccRecipients");
  const lines = [`To: ${to.length ? to.join(", ") : "(unavailable)"}`];
  if (cc.length) lines.push(`CC: ${cc.join(", ")}`);
  return truncate(lines.join("\n"), limit);
}

function relatedInboxText(item: Item, limit = 850): string {
  if (typeof item.related_messages_json !== "string" || !item.related_messages_json) return "";
  let messages: any[];
  try {
    messages = JSON.parse(item.related_messages_json);
    if (!Array.isArray(messages) || !messages.length) return "";
  } catch {
    return "New Email Check: newer Inbox context is present but could not be displayed.";
  }
  const latest = messages[0] || {};
  const latestText = originalEmailText({
    body_content: latest.body_content,
    body_preview: latest.body_preview,
  });
  const relatedAttachments = (() => {
    if (!latest.has_attachments) return "none";
    if (!Array.isArray(latest.attachments)) return "present; filenames unavailable";
    const names = latest.attachments
      .filter((attachment: any) => attachment?.is_inline !== true)
      .map((attachment: any) => String(attachment?.name || "(unnamed)").replace(/\s+/g, " "));
    return names.length ? names.join(", ") : "inline only";
  })();
  return truncate(
    `New Email Check: ${messages.length} newer Inbox message${messages.length === 1 ? "" : "s"} incorporated.\n` +
    `Latest from: ${String(latest.sender_name || latest.sender_email || "Unknown").replace(/\s+/g, " ")}\n` +
    `Latest received: ${latest.received_at || "(unknown)"}\n` +
    `Latest attachments: ${relatedAttachments}\n` +
    `Latest email:\n${quoteLines(latestText || "(empty)")}`,
    limit,
  );
}

function contextText(item: Item, heading: string, draftLabel: string, draft: string): string {
  const sender = truncate(String(item.sender_name || item.sender_email || "Unknown").replace(/\s+/g, " "), 180).replace(/\n/g, " ");
  const subject = truncate(String(item.subject || "(no subject)").replace(/\s+/g, " "), 250).replace(/\n/g, " ");
  const related = relatedInboxText(item);
  const body =
    `From: ${sender}\n` +
    `${recipientText(item, 400)}\n` +
    `Subject: ${subject}\n` +
    `${attachmentText(item, 450)}\n\n` +
    `Original email:\n${quoteLines(truncate(originalEmailText(item) || "(empty)", related ? 700 : 1050))}\n\n` +
    (related ? `${related}\n\n` : "") +
    `${draftLabel}:\n${truncate(draft || "(missing)", 1600)}`;
  return `${heading}\n${neutralizeMarkers(body)}`;
}

export function formatRevisionPrompt(item: Item, token: string): string {
  const p = proposal(item);
  const heading = `✏️ Revision requested\nMailroom revision token: ${token}`;
  const suffix =
    `\n\nWhat would you like changed? Reply directly to this message with your instructions.` +
    `\nFallback: /mr-revise ${token} <your instructions>`;
  return truncate(contextText(item, heading, "Current draft", p.reply_text), 4000 - suffix.length) + suffix;
}

function revisionTokenFromReply(replyBody: unknown): string | null {
  if (typeof replyBody !== "string" || !replyBody) return null;
  const marker = replyBody.match(
    /(?:^|\n)Mailroom revision token:\s*([A-Za-z0-9_-]{8,32})(?:\s|$)/i,
  );
  if (marker) return marker[1];
  // Backward compatibility for prompts sent before the explicit marker existed.
  const legacy = replyBody.match(
    /(?:^|\n)\/mr-revise\s+([A-Za-z0-9_-]{8,32})(?:\s|$)/,
  );
  return legacy?.[1] ?? null;
}

async function runRevision(
  cfg: Config, token: string, instructions: string, accountId: string, chatId: string,
  threadId?: string,
): Promise<Record<string, any>> {
  if (cfg.revisionRunner) return cfg.revisionRunner(token, instructions, accountId, chatId);
  if (!chatId) throw new Error("Mailroom could not resolve the approval chat for this revision");
  const args = [
    "-m", "mailroom.cli", "--db", cfg.dbPath,
    "revise", token, instructions,
    "--account-id", accountId, "--telegram-chat-id", chatId,
  ];
  const resolvedThread = threadId || cfg.telegramThreadId;
  if (resolvedThread) args.push("--telegram-thread-id", resolvedThread);
  const destinations = cfg.telegramDestinations;
  if (destinations && Object.keys(destinations).length) {
    args.push("--telegram-destinations", JSON.stringify(destinations));
  }
  const { stdout } = await execFileAsync(
    cfg.pythonExecutable || "python3",
    args,
    {
      timeout: 660000,
      env: { ...process.env, PYTHONPATH: cfg.pythonPath },
      maxBuffer: 1024 * 1024,
    },
  );
  const result = JSON.parse(stdout);
  if (typeof result !== "object" || result === null || typeof result.ok !== "boolean") {
    throw new Error("Mailroom revision command returned a malformed result");
  }
  return result;
}

/**
 * Claim a natural-language Telegram reply to a Mailroom revision prompt before
 * it reaches an agent. Correlation is fail-closed across token, bot account,
 * chat, sender, channel, production mode, and the exact pending state.
 */
export async function handleRevisionReply(
  event: Record<string, any>, ctx: Record<string, any>, cfg: Config,
): Promise<{ handled: boolean; text?: string } | void> {
  const channel = String(event.channel || ctx.channelId || "").toLowerCase();
  if (channel !== "telegram") return;
  const token = revisionTokenFromReply(event.replyToBody ?? ctx.replyToBody);
  if (!token) return;

  const accountId = String(ctx.accountId || "");
  const conversation = parseTelegramConversationId(String(ctx.conversationId || ""));
  const chatId = conversation.chatId;
  const senderId = String(event.senderId || ctx.senderId || "");
  const instructions = String(event.content || "").trim();
  if (!accountId || !chatId || !senderId) {
    return {
      handled: true,
      text: "Mailroom could not verify the Telegram account, chat, and sender for this revision. Nothing was changed.",
    };
  }
  if (
    event.isAuthorizedSender === false || ctx.isAuthorizedSender === false
    || ctx.auth?.isAuthorizedSender === false
  ) {
    return {
      handled: true,
      text: "Mailroom revision rejected: sender is not authorized. Nothing was changed.",
    };
  }
  const problem = invalidInstructionsMessage(instructions);
  if (problem) return { handled: true, text: problem };

  let db: DatabaseSync | undefined;
  let item: Item | undefined;
  try {
    db = openDb(cfg.dbPath);
    item = db.prepare(`
      SELECT * FROM mail_items
      WHERE callback_token = ? AND run_mode = 'production'
        AND state = 'REVISION_REQUESTED' AND card_channel = 'telegram'
        AND card_account_id = ? AND card_chat_id = ?
    `).get(token, accountId, chatId) as Item | undefined;
  } catch (error: any) {
    logInternalError("revision reply validation failed", error);
    return {
      handled: true,
      text: `Mailroom could not validate this revision prompt. Nothing was changed. ${LOCAL_ERROR_NOTE}`,
    };
  } finally {
    db?.close();
  }
  if (!item) {
    return {
      handled: true,
      text: "This Mailroom revision prompt is stale or does not match this bot and chat. Nothing was changed.",
    };
  }
  const cardThreadId = item.card_thread_id == null || String(item.card_thread_id) === ""
    ? undefined
    : String(item.card_thread_id);
  if (cardThreadId && conversation.threadId !== cardThreadId) {
    return {
      handled: true,
      text: "This Mailroom revision prompt is stale or does not match this bot and chat. Nothing was changed.",
    };
  }
  const cardChatId = String(item.card_chat_id);
  if (isTelegramGroupChat(cardChatId)) {
    if (!groupRevisionReplyAuthorized(ctx, senderId, resolvedRevisionApprovers(cfg))) {
      return {
        handled: true,
        text: "Mailroom revision rejected: sender is not authorized. Nothing was changed.",
      };
    }
  } else if (senderId !== cardChatId) {
    return {
      handled: true,
      text: "Mailroom revision rejected: sender does not match the authorized approval chat. Nothing was changed.",
    };
  }

  try {
    return {
      handled: true,
      text: revisionResultMessage(
        await runRevision(cfg, token, instructions, accountId, chatId, cardThreadId),
      ),
    };
  } catch (error: any) {
    logInternalError("revision reply execution failed", error);
    return { handled: true, text: revisionFailureMessage(error) };
  }
}

/**
 * Validate and run a /mr-revise command. Mirrors the ledger checks the Python
 * CLI applies (token, production mode, pending revision state, bot account) and
 * additionally binds the command to the approval card's authorized chat, so a
 * token leaked outside that chat cannot be replayed elsewhere.
 */
export async function handleRevisionCommand(
  ctx: Record<string, any>, cfg: Config,
): Promise<{ text: string }> {
  const match = String(ctx.args || "").match(/^([A-Za-z0-9_-]{8,32})\s+([\s\S]+)$/);
  if (!match) return { text: "Usage: /mr-revise <token> <instructions>" };
  const token = match[1];
  const instructions = match[2].trim();
  const problem = invalidInstructionsMessage(instructions);
  if (problem) return { text: problem };
  const accountId = String(ctx.accountId || "");
  if (!accountId) return { text: "Mailroom could not resolve the Telegram bot account." };
  const senderId = String(ctx.senderId ?? ctx.from ?? "");
  if (!senderId) {
    return { text: "Mailroom could not verify the Telegram sender for this revision. Nothing was changed." };
  }
  let db: DatabaseSync | undefined;
  let item: Item | undefined;
  try {
    db = openDb(cfg.dbPath);
    item = db.prepare(`
      SELECT * FROM mail_items
      WHERE callback_token = ? AND run_mode = 'production'
        AND state = 'REVISION_REQUESTED' AND card_channel = 'telegram'
        AND card_account_id = ?
    `).get(token, accountId) as Item | undefined;
  } catch (error: any) {
    logInternalError("revision command validation failed", error);
    return {
      text: `Mailroom could not validate this revision token. Nothing was changed. ${LOCAL_ERROR_NOTE}`,
    };
  } finally {
    db?.close();
  }
  if (!item) {
    return { text: "This Mailroom revision token is stale or does not match this bot. Nothing was changed." };
  }
  const cardChatId = String(item.card_chat_id);
  const cardThreadId = item.card_thread_id == null || String(item.card_thread_id) === ""
    ? undefined
    : String(item.card_thread_id);
  const conversation = parseTelegramConversationId(String(ctx.conversationId || ""));
  if (isTelegramGroupChat(cardChatId)) {
    if (!groupRevisionAuthorized(ctx)) {
      return { text: "Mailroom revision rejected: sender is not authorized." };
    }
    if (!conversation.chatId || conversation.chatId !== cardChatId) {
      return { text: "Mailroom revision rejected: sender does not match the authorized approval chat. Nothing was changed." };
    }
    if (cardThreadId && conversation.threadId !== cardThreadId) {
      return { text: "Mailroom revision rejected: sender does not match the authorized approval chat. Nothing was changed." };
    }
  } else {
    if (!ctx.isAuthorizedSender) {
      return { text: "Mailroom revision rejected: sender is not authorized." };
    }
    if (senderId !== cardChatId) {
      return { text: "Mailroom revision rejected: sender does not match the authorized approval chat. Nothing was changed." };
    }
  }
  try {
    return {
      text: revisionResultMessage(
        await runRevision(
          cfg, token, instructions, accountId, cardChatId, cardThreadId,
        ),
      ),
    };
  } catch (error: any) {
    logInternalError("revision command execution failed", error);
    return { text: revisionFailureMessage(error) };
  }
}

export function formatSecondGateText(item: Item, draftId: string): string {
  const p = proposal(item);
  const suffix = "\n\nReview above, then explicitly choose Send.";
  return truncate(contextText(
    item, `✅ Outlook draft created — not sent\nDraft ID: ${draftId}`,
    "Current draft", p.reply_text,
  ), 4000 - suffix.length) + suffix;
}

const secondGateButtons = (token: string) => [[
  { text: "Send", callback_data: `mailroom:send.${token}`, style: "success" },
  { text: "Revise", callback_data: `mailroom:revise.${token}`, style: "primary" },
], [
  { text: "Defer", callback_data: `mailroom:defer.${token}` },
  { text: "Already Responded", callback_data: `mailroom:responded.${token}` },
  { text: "New Email Check", callback_data: `mailroom:new-email-check.${token}` },
  { text: "Cancel", callback_data: `mailroom:cancel.${token}`, style: "danger" },
]];

const respondedOnlyButtons = (token: string) => [[
  { text: "Already Responded", callback_data: `mailroom:responded.${token}` },
]];

async function discardOutlookDraft(cfg: Config, item: Item): Promise<Record<string, any>> {
  if (!item.outlook_draft_id) return { success: true, deleted: false };
  const deleter = cfg.draftDeleter ?? deleteOutlookDraft;
  try {
    return await deleter({
      account: accountForMailbox(cfg, item.mailbox),
      draft_id: item.outlook_draft_id,
    });
  } catch (error: any) {
    logInternalError("Outlook draft cleanup failed", error);
    return { success: false, error: String(error?.message ?? error).slice(0, 500) };
  }
}

export async function handleInteractive(ctx: any, cfg: Config): Promise<{ handled: boolean }> {
  if (!ctx.auth?.isAuthorizedSender) {
    await ctx.respond.reply({ text: "Mailroom approval rejected: sender is not authorized." });
    return { handled: true };
  }
  const dot = String(ctx.callback.payload || "").indexOf(".");
  if (dot < 1) return { handled: false };
  const action = ctx.callback.payload.slice(0, dot);
  const token = ctx.callback.payload.slice(dot + 1);
  if (!/^[A-Za-z0-9_-]{8,32}$/.test(token)) return { handled: false };

  const db = openDb(cfg.dbPath);
  try {
    let item = callbackItem(db, ctx, token);
    if (!item) {
      await ctx.respond.reply({ text: "This Mailroom card is stale or does not match this bot/chat." });
      return { handled: true };
    }

    if (action.startsWith("route-")) {
      const ownerReference = action.slice("route-".length);
      let routingOwners: string[];
      try {
        routingOwners = await resolveRoutingOwnerIds(cfg);
      } catch (error: any) {
        logInternalError("routing-owner validation failed", error);
        await ctx.respond.reply({
          text: `Mailroom could not validate routing owners safely. ${LOCAL_ERROR_NOTE}`,
        });
        return { handled: true };
      }
      const ownerMatches = routingOwners.filter(
        (candidate) => candidate === ownerReference
          || routingOwnerCallbackRef(candidate) === ownerReference,
      );
      if (ownerMatches.length !== 1) {
        await ctx.respond.reply({ text: "That Mailroom routing owner is not allowed." });
        return { handled: true };
      }
      const owner = ownerMatches[0];
      const routed = assignThreadRoute(db, item, owner);
      const routeVerified = routed
        ? (cfg.routeVerifier ?? verifyThreadRoute)(
            db, item.mail_item_id, item.mailbox, item.conversation_id, owner,
          )
        : false;
      await ctx.respond.editMessage({
        text: routeVerified
          ? `✅ Assigned this conversation to ${owner}. Mailroom will draft it on the next cycle.`
          : routed
            ? "⚠️ The routing write completed but its committed state could not be verified. Mailroom will not claim the assignment succeeded; check the routing ledger before retrying."
            : "This routing decision was already handled.",
        buttons: [],
      });
      return { handled: true };
    }

    if (action === "drop") {
      const dropped = claim(
        db, item, ["ROUTING_REVIEW"], "DROPPED", "operator:telegram",
        { card_message_id: null },
      );
      await ctx.respond.editMessage({
        text: dropped ? "🚫 Marked not relevant. This message will not be drafted." : "This routing decision was already handled.",
        buttons: [],
      });
      return { handled: true };
    }

    if (action === "new-email-check") {
      if (!["DRAFT_PROPOSED", "SEND_APPROVAL_PENDING"].includes(item.state)) {
        await ctx.respond.reply({ text: "This item is no longer awaiting an email-freshness check." });
        return { handled: true };
      }
      if (!item.conversation_id || !item.received_at || !item.sender_email) {
        await ctx.respond.reply({
          text: "New Email Check was blocked because the stored message lacks the conversation, timestamp, or sender needed for a safe check. The current draft was not changed.",
        });
        return { handled: true };
      }
      const checker = cfg.threadChecker ?? checkOutlookThreadUpdates;
      let result: Record<string, any>;
      try {
        result = await checker({
          account: accountForMailbox(cfg, item.mailbox),
          conversation_id: item.conversation_id,
          received_after: item.received_at,
          sender_email: item.sender_email,
        });
      } catch (error: any) {
        logInternalError("Outlook thread check failed", error);
        result = { success: false, error: String(error?.message ?? error).slice(0, 500) };
      }
      if (!result.success || !Array.isArray(result.newer_messages)) {
        if (result.error) logInternalError("Outlook thread check returned failure", result.error);
        await ctx.respond.reply({
          text: `New Email Check failed closed. The current draft and approval remain unchanged. ${LOCAL_ERROR_NOTE}`,
        });
        return { handled: true };
      }
      if (result.already_replied) {
        const sent = result.sent_reply || {};
        let replied = markRepliedElsewhere(db, item, item.state, {
          message_id: sent.message_id,
          sent_at: sent.sent_at,
        });
        if (!replied) {
          await ctx.respond.reply({ text: "This item changed while New Email Check was running. Please use the current card." });
          return { handled: true };
        }
        if (replied.outlook_draft_id) {
          const discarded = await discardOutlookDraft(cfg, replied);
          if (!discarded.success) {
            if (discarded.error) logInternalError("stale Outlook draft cleanup returned failure", discarded.error);
            await ctx.respond.editMessage({
              text: `✅ New Email Check found a later Sent Items reply at ${sent.sent_at || "an unknown time"}, so sending is suppressed. The stale Outlook draft still needs cleanup. ${LOCAL_ERROR_NOTE}`,
              buttons: respondedOnlyButtons(token),
            });
            return { handled: true };
          }
          const cleared = claim(
            db, replied, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE", "mailroom-draft-cleanup",
            { outlook_draft_id: null, approval_fingerprint: null, last_error: null },
          );
          if (cleared) replied = cleared;
        }
        await ctx.respond.editMessage({
          text: `✅ New Email Check found a later Sent Items reply at ${sent.sent_at || "an unknown time"}. The Mailroom response was suppressed${item.outlook_draft_id ? " and its stale Outlook draft was removed" : ""}.`,
          buttons: [],
        });
        claim(
          db, replied, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE", "mailroom-card-delivery",
          { card_message_id: null, new_email_checked_at: new Date().toISOString() },
        );
        return { handled: true };
      }
      if (result.newer_messages.length === 0) {
        const checked = claim(
          db, item, [item.state], item.state, "operator:new-email-check",
          { new_email_checked_at: new Date().toISOString(), last_error: null },
        );
        await ctx.respond.reply({
          text: checked
            ? "✅ New Email Check found no later reply in Sent Items and no newer message in this Outlook Inbox conversation. The current draft remains active."
            : "This item changed while New Email Check was running. Please use the current card.",
        });
        return { handled: true };
      }
      const latest = result.newer_messages[0];
      if (
        !latest || typeof latest.message_id !== "string" || !latest.message_id ||
        typeof latest.received_at !== "string" || !latest.received_at ||
        typeof latest.sender_email !== "string" || !latest.sender_email
      ) {
        await ctx.respond.reply({
          text: "New Email Check returned malformed newer-message data. The current draft and approval remain unchanged.",
        });
        return { handled: true };
      }
      const priorState = item.state;
      let refreshing = claim(
        db, item, [priorState], "REVISION_REQUESTED", "operator:new-email-check",
      );
      if (!refreshing) {
        await ctx.respond.reply({ text: "This item changed while New Email Check was running. Please use the current card." });
        return { handled: true };
      }
      if (refreshing.outlook_draft_id) {
        const discarded = await discardOutlookDraft(cfg, refreshing);
        if (!discarded.success) {
          if (discarded.error) logInternalError("refresh draft cleanup returned failure", discarded.error);
          const restored = claim(
            db, refreshing, ["REVISION_REQUESTED"], priorState, "mailroom-draft-cleanup",
            { last_error: discarded.error || "Outlook draft cleanup failed" },
          );
          await ctx.respond.reply({
            text: restored
              ? `A newer related Inbox email was found, but the stale Outlook draft could not be removed. Nothing was requeued and the prior approval remains active. ${LOCAL_ERROR_NOTE}`
              : "The Mailroom state changed while the stale Outlook draft was being removed.",
          });
          return { handled: true };
        }
      }
      let requeued: Item | null;
      try {
        requeued = adoptRelatedMessagesAndRequeue(
          db, refreshing, latest, result.newer_messages,
        );
      } catch (error: any) {
        logInternalError("newer-message consolidation failed", error);
        const cancelled = claim(
          db, refreshing, ["REVISION_REQUESTED"], "CANCELLED", "operator:new-email-check",
          {
            outlook_draft_id: null, approval_fingerprint: null, card_message_id: null,
            last_error: String(error?.message ?? error).slice(0, 2000),
          },
        );
        await ctx.respond.editMessage({
          text: cancelled
            ? `New Email Check found that the newer email already has a conflicting Mailroom workflow. This stale card and its Outlook draft were cancelled; use the newer card. ${LOCAL_ERROR_NOTE}`
            : "The Mailroom state changed while newer email was being consolidated. Please use the current card.",
          buttons: [],
        });
        return { handled: true };
      }
      await ctx.respond.editMessage({
        text: requeued
          ? `♻️ New Email Check found ${result.newer_messages.length} newer related Inbox message${result.newer_messages.length === 1 ? "" : "s"}. The old proposal${refreshing.outlook_draft_id ? " and Outlook draft were" : " was"} invalidated. ${item.draft_owner} will analyze the expanded thread and send a fresh card on the next Mailroom cycle.`
          : "This item changed while New Email Check was running. Please use the current card.",
        buttons: [],
      });
      return { handled: true };
    }

    if (action === "approve") {
      let validationResult: Record<string, any>;
      try {
        validationResult = await validateProposalPolicy(cfg, item);
      } catch (error: any) {
        logInternalError("draft validation failed", error);
        await ctx.respond.reply({
          text: `Draft approval blocked: draft validation was unavailable. Nothing was created. ${LOCAL_ERROR_NOTE}`,
        });
        return { handled: true };
      }
      if (!validationResult.ok) {
        const queued = claim(
          db, item, ["DRAFT_PROPOSED"], "DRAFT_REQUESTED", "mailroom-draft-gate",
          {
            card_message_id: null,
            last_error: `Draft revalidation requested: ${validationResult.violations.join("; ")}`.slice(0, 2000),
          },
        );
        await ctx.respond.editMessage({
          text: queued
            ? "♻️ This proposal failed Mailroom's workflow-neutral draft checks. It was not approved and has been queued for a fresh draft."
            : "This draft decision was already handled.",
          buttons: [],
        });
        return { handled: true };
      }
      item = claim(db, item, ["DRAFT_PROPOSED"], "OUTLOOK_DRAFTING", "operator:telegram");
      if (!item) {
        await ctx.respond.reply({ text: "This draft decision was already handled." });
        return { handled: true };
      }
      const p = proposal(item);
      const creator = cfg.draftCreator ?? createOutlookReplyDraft;
      let result: Record<string, any>;
      try {
        result = await creator({
          account: accountForMailbox(cfg, item.mailbox),
          message_id: item.reply_target_message_id || item.provider_message_id,
          reply: p.reply_text, reply_all: p.reply_all || "auto", signature: "auto",
          conversation_id: item.conversation_id,
          received_after: item.reply_target_received_at || item.received_at,
          sender_email: item.reply_target_sender_email || item.sender_email,
        });
      } catch (error: any) {
        logInternalError("Outlook draft creation failed", error);
        result = { success: false, error: String(error?.message ?? error).slice(0, 500) };
      }
      if (result.already_replied) {
        const replied = markRepliedElsewhere(db, item, "OUTLOOK_DRAFTING", result);
        await ctx.respond.editMessage({
          text: `✅ Suppressed: a reply was already sent at ${result.sent_at || "an unknown time"}. No Outlook draft was created.`,
          buttons: [],
        });
        if (replied) {
          claim(
            db, replied, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE", "mailroom-card-delivery",
            { card_message_id: null },
          );
        }
        return { handled: true };
      }
      if (!result.success || !result.draft_id || !result.approval_token) {
        if (result.error) logInternalError("Outlook draft creation returned failure", result.error);
        failState(db, item.mail_item_id, "OUTLOOK_DRAFTING", "ERROR", result.error || "Draft fingerprint unavailable");
        await ctx.respond.editMessage({ text: `❌ Outlook draft creation failed. Nothing was sent. ${LOCAL_ERROR_NOTE}`, buttons: [] });
        return { handled: true };
      }
      item = finishDraft(db, item, result);
      await ctx.respond.editMessage({
        text: formatSecondGateText(item, result.draft_id),
        buttons: secondGateButtons(token),
      });
      claim(
        db, item, ["SEND_APPROVAL_PENDING"], "SEND_APPROVAL_PENDING", "mailroom-card-delivery",
        { card_message_id: String(ctx.callback.messageId), last_error: null },
      );
      return { handled: true };
    }

    if (action === "send") {
      try {
        const validationResult = await validateProposalPolicy(cfg, item);
        if (!validationResult.ok) {
          await ctx.respond.reply({
            text: "Send blocked: this Outlook draft no longer passes Mailroom's workflow-neutral draft checks. Choose Revise to generate a replacement.",
          });
          return { handled: true };
        }
      } catch (error: any) {
        logInternalError("send draft validation failed", error);
        await ctx.respond.reply({
          text: `Send blocked: draft validation was unavailable. Nothing was sent. ${LOCAL_ERROR_NOTE}`,
        });
        return { handled: true };
      }
      item = claim(db, item, ["SEND_APPROVAL_PENDING"], "SENDING", "operator:telegram");
      if (!item) {
        await ctx.respond.reply({ text: "This send decision was already handled." });
        return { handled: true };
      }
      const sender = cfg.draftSender ?? sendOutlookDraft;
      let result: Record<string, any>;
      try {
        result = await sender({
          account: accountForMailbox(cfg, item.mailbox), draft_id: item.outlook_draft_id,
          approval_token: item.approval_fingerprint,
          conversation_id: item.conversation_id,
          received_after: item.reply_target_received_at || item.received_at,
          sender_email: item.reply_target_sender_email || item.sender_email,
        });
      } catch (error: any) {
        logInternalError("Outlook send request failed", error);
        // A throw here means the send outcome is genuinely unknown — treat it
        // as attempted so the item lands in SEND_OUTCOME_UNKNOWN, never retried.
        result = {
          success: false, send_attempted: true,
          error: String(error?.message ?? error).slice(0, 500),
        };
      }
      if (result.already_replied) {
        let replied = markRepliedElsewhere(db, item, "SENDING", result);
        if (replied?.outlook_draft_id) {
          const discarded = await discardOutlookDraft(cfg, replied);
          if (!discarded.success) {
            if (discarded.error) logInternalError("post-suppression draft cleanup returned failure", discarded.error);
            await ctx.respond.editMessage({
              text: `✅ Send suppressed because another reply was already sent at ${result.sent_at || "an unknown time"}. The stale Outlook draft could not be removed; choose Already Responded to retry cleanup. ${LOCAL_ERROR_NOTE}`,
              buttons: respondedOnlyButtons(token),
            });
            return { handled: true };
          }
          replied = claim(
            db, replied, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE",
            "mailroom-draft-cleanup",
            { outlook_draft_id: null, approval_fingerprint: null, last_error: null },
          );
        }
        await ctx.respond.editMessage({
          text: `✅ Send suppressed: another reply was already sent at ${result.sent_at || "an unknown time"}. The stale Mailroom Outlook draft was removed.`,
          buttons: [],
        });
        if (replied) {
          claim(
            db, replied, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE", "mailroom-card-delivery",
            { card_message_id: null },
          );
        }
        return { handled: true };
      }
      if (!result.success) {
        if (result.error) logInternalError("Outlook send returned failure", result.error);
        if (sendFailureState(result) === "SEND_APPROVAL_PENDING") {
          const restored = claim(
            db, item, ["SENDING"], "SEND_APPROVAL_PENDING", "mailroom-plugin",
            { last_error: result.error || "Pre-send safety check failed" },
          );
          await ctx.respond.editMessage({
            text: restored
              ? `⚠️ Nothing was sent. A pre-send safety check failed. ${LOCAL_ERROR_NOTE}\n\nRetry Send or choose Revise.`
              : "The send state changed before Mailroom could restore approval.",
            buttons: restored ? secondGateButtons(token) : [],
          });
          return { handled: true };
        }
        failState(db, item.mail_item_id, "SENDING", "SEND_OUTCOME_UNKNOWN", result.error || "Unknown send failure");
        await ctx.respond.editMessage({ text: `⚠️ Send outcome is unknown. Do not retry automatically. ${LOCAL_ERROR_NOTE}`, buttons: [] });
        return { handled: true };
      }
      const accepted = claim(db, item, ["SENDING"], "SEND_ACCEPTED", "operator:telegram", {
        last_error: null, send_accepted_at: new Date().toISOString(),
      });
      if (!accepted) throw new Error("Send succeeded but ledger finalization lost its claim");
      await ctx.respond.editMessage({ text: "✅ Outlook accepted the send. Mailroom recorded SEND_ACCEPTED.", buttons: [] });
      return { handled: true };
    }

    if (action === "revise") {
      const priorState = item.state;
      let revised = item.state === "REVISION_REQUESTED"
        ? item
        : claim(
          db, item, ["DRAFT_PROPOSED", "SEND_APPROVAL_PENDING"],
          "REVISION_REQUESTED", "operator:telegram",
        );
      if (!revised) {
        await ctx.respond.reply({ text: "This item is no longer awaiting revision." });
        return { handled: true };
      }
      if (revised.outlook_draft_id) {
        const discarded = await discardOutlookDraft(cfg, revised);
        if (!discarded.success) {
          if (discarded.error) logInternalError("revision draft cleanup returned failure", discarded.error);
          const restored = claim(
            db, revised, ["REVISION_REQUESTED"], priorState,
            "mailroom-draft-cleanup", { last_error: discarded.error || "Outlook draft cleanup failed" },
          );
          await ctx.respond.reply({
            text: restored
              ? `Revision was not started because the superseded Outlook draft could not be removed. The prior approval remains active. ${LOCAL_ERROR_NOTE}`
              : "The Mailroom state changed while the superseded Outlook draft was being removed.",
          });
          return { handled: true };
        }
        const cleared = claim(
          db, revised, ["REVISION_REQUESTED"], "REVISION_REQUESTED",
          "mailroom-draft-cleanup",
          { outlook_draft_id: null, approval_fingerprint: null, last_error: null },
        );
        if (!cleared) {
          await ctx.respond.reply({ text: "This item was handled while its superseded Outlook draft was being removed." });
          return { handled: true };
        }
        revised = cleared;
      }
      try {
        await ctx.respond.reply({ text: formatRevisionPrompt(revised, token) });
      } catch (error: any) {
        claim(
          db, revised, ["REVISION_REQUESTED"], "REVISION_REQUESTED",
          "mailroom-card-delivery",
          { last_error: `Revision prompt delivery failed: ${String(error?.message ?? error)}`.slice(0, 2000) },
        );
        throw error;
      }
      try {
        await ctx.respond.editButtons({ buttons: respondedOnlyButtons(token) });
      } catch (error: any) {
        claim(
          db, revised, ["REVISION_REQUESTED"], "REVISION_REQUESTED",
          "mailroom-card-delivery",
          { last_error: `Revision button cleanup failed: ${String(error?.message ?? error)}`.slice(0, 2000) },
        );
      }
      return { handled: true };
    }

    if (action === "responded") {
      if (item.state === "REPLIED_ELSEWHERE") {
        let resolved = item;
        if (resolved.outlook_draft_id) {
          const discarded = await discardOutlookDraft(cfg, resolved);
          if (!discarded.success) {
            if (discarded.error) logInternalError("already-responded cleanup returned failure", discarded.error);
            await ctx.respond.reply({
              text: `Mailroom recorded Already Responded, but the stale Outlook draft still needs removal. Retry this button after the cleanup error is resolved. ${LOCAL_ERROR_NOTE}`,
            });
            return { handled: true };
          }
          const cleared = claim(
            db, resolved, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE",
            "mailroom-draft-cleanup",
            { outlook_draft_id: null, approval_fingerprint: null, last_error: null },
          );
          if (cleared) resolved = cleared;
        }
        await ctx.respond.editMessage({
          text: "✅ Marked Already Responded. Mailroom will not draft or send a response for this email.",
          buttons: [],
        });
        claim(
          db, resolved, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE", "mailroom-card-delivery",
          { card_message_id: null },
        );
        return { handled: true };
      }
      const priorState = item.state;
      let responded = claim(
        db, item, ["DRAFT_PROPOSED", "SEND_APPROVAL_PENDING", "REVISION_REQUESTED"],
        "REPLIED_ELSEWHERE", "operator:already-responded",
        { replied_sent_id: null, replied_sent_at: null, last_error: null },
      );
      if (responded?.outlook_draft_id) {
        const discarded = await discardOutlookDraft(cfg, responded);
        if (!discarded.success) {
          if (discarded.error) logInternalError("already-responded transition cleanup returned failure", discarded.error);
          const restored = claim(
            db, responded, ["REPLIED_ELSEWHERE"], priorState,
            "mailroom-draft-cleanup", { last_error: discarded.error || "Outlook draft cleanup failed" },
          );
          await ctx.respond.reply({
            text: restored
              ? `Already Responded was not recorded because the Mailroom Outlook draft could not be removed. The prior decision remains active. ${LOCAL_ERROR_NOTE}`
              : "The Mailroom state changed while the Outlook draft was being removed.",
          });
          return { handled: true };
        }
        responded = claim(
          db, responded, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE",
          "mailroom-draft-cleanup",
          { outlook_draft_id: null, approval_fingerprint: null, last_error: null },
        );
      }
      await ctx.respond.editMessage({
        text: responded
          ? "✅ Marked Already Responded. Mailroom will not draft or send a response for this email."
          : "This item was already handled.",
        buttons: [],
      });
      if (responded) {
        claim(
          db, responded, ["REPLIED_ELSEWHERE"], "REPLIED_ELSEWHERE", "mailroom-card-delivery",
          { card_message_id: null },
        );
      }
      return { handled: true };
    }

    if (action === "defer") {
      await ctx.respond.editButtons({ buttons: [[
        { text: "1 hour", callback_data: `mailroom:defer1h.${token}` },
        { text: "Tomorrow", callback_data: `mailroom:defer1d.${token}` },
        { text: "Next week", callback_data: `mailroom:defer7d.${token}` },
      ]] });
      return { handled: true };
    }

    if (["defer1h", "defer1d", "defer7d"].includes(action)) {
      const milliseconds = action === "defer1h" ? 3600000 : action === "defer1d" ? 86400000 : 604800000;
      const deferred = claim(
        db, item, ["DRAFT_PROPOSED", "SEND_APPROVAL_PENDING"], "DEFERRED", "operator:telegram",
        {
          deferred_until: new Date(Date.now() + milliseconds).toISOString(),
          deferred_from_state: item.state,
          card_message_id: null,
        },
      );
      await ctx.respond.editMessage({
        text: deferred ? `⏰ Deferred until ${deferred.deferred_until}.` : "This item was already handled.",
        buttons: [],
      });
      return { handled: true };
    }

    if (action === "deny") {
      const denied = claim(db, item, ["DRAFT_PROPOSED"], "DENIED_MESSAGE", "operator:telegram", {
        denied_content_hash: item.content_hash,
      });
      await ctx.respond.editMessage({ text: denied ? "🚫 Response denied for this message." : "This item was already handled.", buttons: [] });
      return { handled: true };
    }

    if (action === "cancel") {
      const cancelled = claim(db, item, ["SEND_APPROVAL_PENDING"], "CANCELLED", "operator:telegram");
      await ctx.respond.editMessage({ text: cancelled ? "Cancelled. The Outlook draft remains in Drafts and was not sent." : "This item was already handled.", buttons: [] });
      return { handled: true };
    }
    return { handled: false };
  } finally {
    db.close();
  }
}

const plugin: any = definePluginEntry({
  id: "mailroom",
  name: "Mailroom",
  description: "Restart-safe Telegram approval cards for Outlook drafts and sends.",
  register(api: any) {
    const cfg = resolveConfig(api.pluginConfig);
    api.registerCli(
      async ({ program }: any) => {
        const { registerMailroomCli } = await import("./cli.js");
        registerMailroomCli({ program, cfg });
      },
      {
        descriptors: [{
          name: "mailroom",
          description: "Configure Mailroom routing owners and responsibility profiles",
          hasSubcommands: true,
        }],
      },
    );
    if (api.registrationMode === undefined || api.registrationMode === "full") {
      publishRoutingOwnerPolicy(cfg);
    }
    configureOutlookAdapter({
      connectionsPath: cfg.connectionsPath,
      signaturesPath: cfg.signaturesPath,
      accountAliases: cfg.accounts ?? {},
    });
    api.registerInteractiveHandler({
      channel: "telegram",
      namespace: "mailroom",
      handler: async (ctx: any) => handleInteractive(ctx, cfg),
    });
    api.on(
      "before_dispatch",
      async (event: any, ctx: any) => handleRevisionReply(event, ctx, cfg),
      { priority: 100 },
    );
    api.registerCommand({
      name: "mr-revise",
      description: "Revise a pending Mailroom email proposal.",
      channels: ["telegram"],
      acceptsArgs: true,
      requireAuth: true,
      handler: async (ctx: any) => handleRevisionCommand(ctx, cfg),
    });
  },
});

export default plugin;
