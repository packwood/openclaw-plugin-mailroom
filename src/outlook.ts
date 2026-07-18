import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

/**
 * Outlook draft / reply / send services via the Maton Graph gateway,
 * gateway, with correct Calibri styling, quoted-chain threading, and per-account
 * HTML signatures. Encodes the two email playbooks so agents don't hand-roll the
 * fragile Graph mechanics.
 *
 * Read-only-ish: draft + reply CREATE gated drafts (safe, land in Outlook Drafts).
 * email_outlook_send is OPTIONAL (must be allowlisted) and is the only tool that
 * actually sends. The approval policy lives in the Mailroom state machine.
 */

const GRAPH_ROOT = "https://gateway.maton.ai/outlook/v1.0";
const GATEWAY = `${GRAPH_ROOT}/me/messages`;
const OPENCLAW_ROOT = join(homedir(), ".openclaw");
const DEFAULT_CONNECTIONS_PATH = join(OPENCLAW_ROOT, "shared", "connections.json");
const DEFAULT_SIGNATURES_PATH = fileURLToPath(new URL("../signatures", import.meta.url));

const BODY_PT = 12;
const FONT_DIV = `<div style="font-family: Calibri, Arial, sans-serif; font-size: ${BODY_PT}pt; color: #000000;">`;

type Cfg = { internalDomains?: string[] };

type OutlookAdapterConfig = {
  connectionsPath?: string;
  signaturesPath?: string;
  accountAliases?: Record<string, string>;
};

let adapterConfig: OutlookAdapterConfig = {};

/** Configure host-specific paths and optional mailbox aliases at plugin startup. */
export function configureOutlookAdapter(config: OutlookAdapterConfig): void {
  adapterConfig = { ...config };
}

// --------------------------------------------------------------------------- //
// helpers
// --------------------------------------------------------------------------- //
function apiKey(): string {
  const k = process.env.MATON_API_KEY;
  if (!k) throw new Error("MATON_API_KEY is not set in the gateway environment");
  return k;
}

function resolveConnection(account: string): { email: string; connectionId: string } {
  const email = adapterConfig.accountAliases?.[account] ?? account;
  const connectionsPath = adapterConfig.connectionsPath || DEFAULT_CONNECTIONS_PATH;
  const parsed = JSON.parse(readFileSync(connectionsPath, "utf8"));
  const conns = (Array.isArray(parsed) ? parsed : parsed.connections) as Array<{
    app?: string;
    account?: string;
    connection_id: string;
    status?: string;
  }>;
  const hit = conns.find(
    (c) => c.app === "outlook" && (c.account || "").toLowerCase() === email.toLowerCase(),
  );
  if (!hit) throw new Error(`no Maton outlook connection found for ${email}`);
  if (hit.status && hit.status !== "ACTIVE") {
    throw new Error(`Maton connection for ${email} is ${hit.status}, not ACTIVE`);
  }
  return { email, connectionId: hit.connection_id };
}

function loadSignature(account: string): string | null {
  try {
    const signaturesPath = adapterConfig.signaturesPath || DEFAULT_SIGNATURES_PATH;
    const safeName = account.replace(/[^A-Za-z0-9_.@-]/g, "_");
    return readFileSync(join(signaturesPath, `${safeName}.html`), "utf8").trim();
  } catch {
    return null; // no signature stored for this account
  }
}

/**
 * Normalize LITERAL escaped-newline sequences ("\n", "\r\n", "\r" as backslash+letter)
 * into real newlines. Agents/JSON frequently double-escape newlines in tool args, which
 * would otherwise render as visible "\n" text in the email body.
 */
function unescapeNewlines(s: string): string {
  return s.replace(/\\r\\n|\\r|\\n/g, "\n");
}

function escapeHtml(text: string): string {
  return unescapeNewlines(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\n/g, "<br>\n");
}

/** Body may be given as raw HTML (body_html) or plain text (body) — HTML wins. */
function bodyToHtml(bodyHtml?: string, body?: string): string {
  if (bodyHtml && bodyHtml.trim()) return bodyHtml;
  return escapeHtml(body ?? "");
}

function isExternal(recipients: string[], cfg: Cfg): boolean {
  const internal = (cfg.internalDomains ?? []).map((d) => d.toLowerCase());
  return recipients.some((r) => {
    const at = r.lastIndexOf("@");
    if (at < 0) return true;
    const dom = r.slice(at + 1).toLowerCase();
    return !internal.includes(dom);
  });
}

/** Decide whether to attach the signature. mode: auto | on | off. */
function wantSignature(mode: string, isReply: boolean, recipients: string[], cfg: Cfg): boolean {
  if (mode === "on") return true;
  if (mode === "off") return false;
  // auto: new external messages only — never on replies (sig is already down-thread).
  if (isReply) return false;
  return isExternal(recipients, cfg);
}

async function maton(
  method: string,
  path: string,
  connectionId: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<{ status: number; json: any; text: string }> {
  const res = await fetch(`${GATEWAY}${path}`, {
    method,
    headers: {
      Authorization: `Bearer ${apiKey()}`,
      "Maton-Connection": connectionId,
      "Content-Type": "application/json",
    },
    body: body === undefined ? (["GET", "DELETE"].includes(method) ? undefined : "{}") : JSON.stringify(body),
    signal,
  });
  const text = await res.text();
  let json: any = null;
  try {
    json = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON (e.g. 202 empty) */
  }
  return { status: res.status, json, text };
}

async function matonGraphGet(
  pathOrUrl: string,
  connectionId: string,
  signal?: AbortSignal,
): Promise<{ status: number; json: any; text: string }> {
  const res = await fetch(normalizeGraphUrl(pathOrUrl), {
    method: "GET",
    headers: {
      Authorization: `Bearer ${apiKey()}`,
      "Maton-Connection": connectionId,
      Accept: "application/json",
    },
    signal,
  });
  const text = await res.text();
  let json: any = null;
  try { json = text ? JSON.parse(text) : null; } catch { /* non-JSON */ }
  return { status: res.status, json, text };
}

export function normalizeGraphUrl(pathOrUrl: string): string {
  if (pathOrUrl.startsWith("/")) return `${GRAPH_ROOT}${pathOrUrl}`;
  const parsed = new URL(pathOrUrl);
  if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.port) {
    throw new Error("Unsafe Sent Items continuation URL");
  }
  if (parsed.hostname === "graph.microsoft.com") {
    const path = parsed.pathname.replace(/^\/v1\.0/, "");
    return `${GRAPH_ROOT}${path}${parsed.search}`;
  }
  if (parsed.hostname === "gateway.maton.ai" && parsed.pathname.startsWith("/outlook/v1.0/")) {
    return parsed.toString();
  }
  throw new Error(`Untrusted Sent Items continuation host: ${parsed.hostname}`);
}

const enc = (id: string) => encodeURIComponent(id);
const rcpts = (addrs?: string[]) =>
  (addrs ?? []).map((a) => ({ emailAddress: { address: a } }));

async function addAttachments(
  draftId: string,
  connectionId: string,
  paths: string[],
  signal?: AbortSignal,
): Promise<string | null> {
  for (const p of paths) {
    let bytes: Buffer;
    try {
      bytes = readFileSync(p);
    } catch {
      return `attachment not found: ${p}`;
    }
    if (bytes.length > 3 * 1024 * 1024) {
      return `attachment >3MB needs a Graph upload session: ${p}`;
    }
    const name = p.split("/").pop() || "attachment";
    const att = {
      "@odata.type": "#microsoft.graph.fileAttachment",
      name,
      contentBytes: bytes.toString("base64"),
    };
    const r = await maton("POST", `/${enc(draftId)}/attachments`, connectionId, att, signal);
    if (![200, 201].includes(r.status)) {
      return `attach ${name} failed: HTTP ${r.status} — ${r.text.slice(0, 200)}`;
    }
  }
  return null;
}

/** Fetch a draft's canonical recipients/subject/body for fingerprinting. */
async function fetchDraft(draftId: string, connectionId: string, signal?: AbortSignal) {
  return maton(
    "GET",
    `/${enc(draftId)}?$select=toRecipients,ccRecipients,bccRecipients,subject,body`,
    connectionId,
    undefined,
    signal,
  );
}

/**
 * Stable content fingerprint of a draft (recipients + subject + body +
 * attachments). Binds email_outlook_send to the approved content: both draft-time
 * and send-time read the SAME canonical stored draft, so send refuses if the draft
 * drifted since it was approved.
 */
function fingerprint(account: string, msg: any, attachments: any[] = []): string {
  const addrs = (["toRecipients", "ccRecipients", "bccRecipients"] as const)
    .flatMap((k) => (msg?.[k] ?? []).map((r: any) => (r?.emailAddress?.address ?? "").toLowerCase()))
    .filter(Boolean)
    .sort();
  const subject = String(msg?.subject ?? "");
  const body = String(msg?.body?.content ?? "");
  const atts = attachments
    .map((a: any) => `${a?.name ?? ""}|${a?.size ?? ""}|${a?.contentType ?? ""}`)
    .sort();
  return createHash("sha256")
    .update(`${account}\n${addrs.join(",")}\n${subject}\n${body}\n${atts.join(";")}`)
    .digest("hex")
    .slice(0, 24);
}

/**
 * Fetch a draft's canonical content AND its attachment set, then compute the
 * approval fingerprint over both. Returns ok:false if EITHER read fails, so send
 * fails closed (refuses) rather than sending an unverified draft.
 */
async function draftFingerprint(
  account: string,
  draftId: string,
  connectionId: string,
  signal?: AbortSignal,
): Promise<{ ok: boolean; token?: string; status: number }> {
  const msg = await fetchDraft(draftId, connectionId, signal);
  if (![200, 201].includes(msg.status)) return { ok: false, status: msg.status };
  const att = await maton(
    "GET",
    `/${enc(draftId)}/attachments?$select=name,size,contentType`,
    connectionId,
    undefined,
    signal,
  );
  if (![200, 201].includes(att.status)) return { ok: false, status: att.status };
  return { ok: true, token: fingerprint(account, msg.json, att.json?.value ?? []), status: 200 };
}

/**
 * Splice the reply block into the existing quoted chain WITHOUT nesting a second
 * full HTML document. If the chain is a full document, insert right after its
 * <body>; otherwise treat it as a fragment.
 */
function combineReplyHtml(replyBlock: string, existing: string): string {
  const bodyOpen = existing.match(/<body[^>]*>/i);
  if (bodyOpen && bodyOpen.index !== undefined) {
    const at = bodyOpen.index + bodyOpen[0].length;
    return existing.slice(0, at) + replyBlock + "<br>" + existing.slice(at);
  }
  return `${replyBlock}<br>${existing}`;
}

export type OutlookReplyDraftInput = {
  account: string;
  message_id: string;
  reply?: string;
  reply_html?: string;
  reply_all?: "auto" | "all" | "sender";
  attachments?: string[];
  signature?: "auto" | "on" | "off";
  conversation_id?: string;
  received_after?: string;
  sender_email?: string;
};

export type OutlookSendInput = {
  account: string;
  draft_id: string;
  approval_token: string;
  conversation_id?: string;
  received_after?: string;
  sender_email?: string;
};

export type OutlookReplyGuardInput = {
  account: string;
  conversation_id: string;
  received_after: string;
  sender_email: string;
};

export type OutlookThreadUpdateInput = OutlookReplyGuardInput;

export type OutlookInboxMessage = {
  message_id: string;
  internet_message_id: string | null;
  conversation_id: string;
  received_at: string;
  subject: string | null;
  sender_email: string;
  sender_name: string | null;
  to_recipients: Array<{ name: string | null; address: string }>;
  cc_recipients: Array<{ name: string | null; address: string }>;
  body_preview: string;
  body_content: string;
  body_content_type: string | null;
  has_attachments: boolean;
  attachments: Array<{
    attachment_id: string | null;
    name: string;
    size: number | null;
    is_inline: boolean;
    content_type: string | null;
  }>;
};

function graphRecipients(value: unknown): Array<{ name: string | null; address: string }> {
  if (!Array.isArray(value)) return [];
  return value.flatMap((recipient: any) => {
    const address = String(recipient?.emailAddress?.address || "").trim();
    if (!address) return [];
    const name = String(recipient?.emailAddress?.name || "").trim();
    return [{ name: name || null, address }];
  });
}

export function selectNewerInboxMessages(
  messages: unknown, conversationId: string, receivedAfter: string,
): OutlookInboxMessage[] {
  if (!Array.isArray(messages)) throw new Error("Inbox response is missing a message value array");
  const cutoff = Date.parse(receivedAfter);
  if (!Number.isFinite(cutoff)) throw new Error("received_after is not a valid timestamp");
  const selected: OutlookInboxMessage[] = [];
  for (const message of messages) {
    const received = Date.parse(String(message?.receivedDateTime || ""));
    const senderEmail = String(message?.from?.emailAddress?.address || "").trim();
    if (
      message?.isDraft !== true && message?.conversationId === conversationId &&
      typeof message?.id === "string" && message.id && senderEmail &&
      Number.isFinite(received) && received > cutoff
    ) {
      selected.push({
        message_id: message.id,
        internet_message_id: typeof message.internetMessageId === "string" ? message.internetMessageId : null,
        conversation_id: conversationId,
        received_at: message.receivedDateTime,
        subject: typeof message.subject === "string" ? message.subject : null,
        sender_email: senderEmail,
        sender_name: String(message?.from?.emailAddress?.name || "").trim() || null,
        to_recipients: graphRecipients(message.toRecipients),
        cc_recipients: graphRecipients(message.ccRecipients),
        body_preview: typeof message.bodyPreview === "string" ? message.bodyPreview.slice(0, 4000) : "",
        body_content: typeof message?.body?.content === "string" ? message.body.content.slice(0, 30000) : "",
        body_content_type: typeof message?.body?.contentType === "string" ? message.body.contentType : null,
        has_attachments: message.hasAttachments === true,
        attachments: [],
      });
    }
  }
  return selected.sort((a, b) => Date.parse(b.received_at) - Date.parse(a.received_at));
}

export function selectSentReply(
  messages: unknown, conversationId: string, receivedAfter: string, senderEmail: string,
): Record<string, any> | null {
  if (!Array.isArray(messages)) throw new Error("Sent Items response is missing a message value array");
  const cutoff = Date.parse(receivedAfter);
  if (!Number.isFinite(cutoff)) throw new Error("received_after is not a valid timestamp");
  const sender = senderEmail.toLowerCase();
  for (const message of messages) {
    const sent = Date.parse(String(message?.sentDateTime || ""));
    const recipients = ["toRecipients", "ccRecipients"]
      .flatMap((field) => message?.[field] ?? [])
      .map((recipient: any) => String(recipient?.emailAddress?.address || "").toLowerCase());
    if (
      message?.isDraft !== true && message?.conversationId === conversationId &&
      typeof message?.id === "string" && Number.isFinite(sent) && sent > cutoff &&
      recipients.includes(sender)
    ) {
      return {
        message_id: message.id, sent_at: message.sentDateTime,
        subject: message.subject ?? null,
      };
    }
  }
  return null;
}

async function findOutlookSentReplyResolved(
  p: OutlookReplyGuardInput,
  connectionId: string,
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  const cutoff = new Date(p.received_after);
  if (!Number.isFinite(cutoff.getTime())) {
    return { success: false, error: "received_after is not a valid timestamp" };
  }
  const escapedConversation = p.conversation_id.replaceAll("'", "''");
  const params = new URLSearchParams({
    "$select": "id,conversationId,sentDateTime,subject,isDraft,toRecipients,ccRecipients",
    "$filter": `sentDateTime gt ${cutoff.toISOString()} and conversationId eq '${escapedConversation}'`,
    "$orderby": "sentDateTime desc",
    "$top": "10",
  });
  let url: string | null = `/me/mailFolders/sentitems/messages?${params.toString()}`;
  for (let page = 0; url && page < 20; page += 1) {
    const result = await matonGraphGet(url, connectionId, signal);
    if (result.status !== 200) {
      return {
        success: false,
        error: `Sent Items guard failed: HTTP ${result.status} — ${result.text.slice(0, 200)}`,
      };
    }
    if (!Array.isArray(result.json?.value)) {
      return { success: false, error: "Sent Items response is missing a message value array" };
    }
    const reply = selectSentReply(
      result.json.value, p.conversation_id, p.received_after, p.sender_email,
    );
    if (reply) return { success: true, found: true, ...reply };
    const next = result.json?.["@odata.nextLink"];
    if (next !== undefined && typeof next !== "string") {
      return { success: false, error: "Sent Items response has an invalid @odata.nextLink" };
    }
    url = next || null;
  }
  if (url) return { success: false, error: "Sent Items guard exceeded 20 pages" };
  return { success: true, found: false };
}

async function runReplyGuard(
  p: {
    account: string; conversation_id?: string; received_after?: string; sender_email?: string;
  },
  connectionId: string,
  signal?: AbortSignal,
): Promise<Record<string, any> | null> {
  if (!p.conversation_id && !p.received_after && !p.sender_email) return null;
  if (!p.conversation_id || !p.received_after || !p.sender_email) {
    return { success: false, error: "Sent Items guard requires conversation_id, received_after, and sender_email" };
  }
  const guard = await findOutlookSentReplyResolved({
    account: p.account, conversation_id: p.conversation_id,
    received_after: p.received_after, sender_email: p.sender_email,
  }, connectionId, signal);
  if (!guard.success) return guard;
  if (guard.found) return { ...guard, success: false, already_replied: true };
  return null;
}

export async function findOutlookSentReply(
  p: OutlookReplyGuardInput,
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  try {
    const { connectionId } = resolveConnection(p.account);
    return await findOutlookSentReplyResolved(p, connectionId, signal);
  } catch (error: any) {
    return { success: false, error: String(error?.message ?? error) };
  }
}

async function findOutlookInboxUpdatesResolved(
  p: OutlookThreadUpdateInput,
  connectionId: string,
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  const cutoff = new Date(p.received_after);
  if (!Number.isFinite(cutoff.getTime())) {
    return { success: false, error: "received_after is not a valid timestamp" };
  }
  const escapedConversation = p.conversation_id.replaceAll("'", "''");
  const params = new URLSearchParams({
    "$select": "id,internetMessageId,conversationId,receivedDateTime,subject,from,toRecipients,ccRecipients,bodyPreview,body,isDraft,hasAttachments",
    "$filter": `receivedDateTime gt ${cutoff.toISOString()} and conversationId eq '${escapedConversation}'`,
    "$orderby": "receivedDateTime desc",
    "$top": "25",
  });
  let url: string | null = `/me/mailFolders/inbox/messages?${params.toString()}`;
  const messages: OutlookInboxMessage[] = [];
  for (let page = 0; url && page < 20; page += 1) {
    const result = await matonGraphGet(url, connectionId, signal);
    if (result.status !== 200) {
      return {
        success: false,
        error: `Inbox update check failed: HTTP ${result.status} — ${result.text.slice(0, 200)}`,
      };
    }
    if (!Array.isArray(result.json?.value)) {
      return { success: false, error: "Inbox response is missing a message value array" };
    }
    messages.push(...selectNewerInboxMessages(
      result.json.value, p.conversation_id, p.received_after,
    ));
    const next = result.json?.["@odata.nextLink"];
    if (next !== undefined && typeof next !== "string") {
      return { success: false, error: "Inbox response has an invalid @odata.nextLink" };
    }
    url = next || null;
  }
  if (url) return { success: false, error: "Inbox update check exceeded 20 pages" };
  const byId = new Map(messages.map((message) => [message.message_id, message]));
  const unique = [...byId.values()].sort(
    (a, b) => Date.parse(b.received_at) - Date.parse(a.received_at),
  );
  if (unique.length > 100) {
    return { success: false, error: "Inbox update check exceeded 100 related messages" };
  }
  for (const message of unique) {
    if (!message.has_attachments) continue;
    const params = new URLSearchParams({
      "$select": "id,name,size,isInline,contentType",
      "$top": "100",
    });
    const result = await matonGraphGet(
      `/me/messages/${enc(message.message_id)}/attachments?${params.toString()}`,
      connectionId,
      signal,
    );
    if (result.status !== 200) {
      return {
        success: false,
        error: `Inbox attachment metadata check failed: HTTP ${result.status} — ${result.text.slice(0, 200)}`,
      };
    }
    if (!Array.isArray(result.json?.value)) {
      return { success: false, error: "Inbox attachment response is missing a value array" };
    }
    if (result.json?.["@odata.nextLink"]) {
      return { success: false, error: "Inbox message has more than 100 attachments" };
    }
    message.attachments = result.json.value.flatMap((attachment: any) => {
      const name = String(attachment?.name || "").trim();
      if (!name) return [];
      return [{
        attachment_id: typeof attachment.id === "string" ? attachment.id : null,
        name: name.slice(0, 500),
        size: typeof attachment.size === "number" && Number.isFinite(attachment.size)
          ? attachment.size : null,
        is_inline: attachment.isInline === true,
        content_type: typeof attachment.contentType === "string" ? attachment.contentType : null,
      }];
    });
  }
  return {
    success: true,
    messages: unique,
  };
}

function inboxSnapshotKey(messages: OutlookInboxMessage[]): string {
  return JSON.stringify(messages.map((message) => [
    message.message_id, message.received_at, message.sender_email,
  ]));
}

/**
 * Stabilize the Inbox/Sent view before deciding whether an existing reply
 * suppresses redrafting. Graph does not provide a cross-folder transaction, so
 * reread Inbox after each Sent query and fail closed if the thread keeps moving.
 */
export async function stabilizeOutlookThreadUpdates(
  p: OutlookThreadUpdateInput,
  fetchInbox: () => Promise<Record<string, any>>,
  fetchSent: (guard: OutlookReplyGuardInput) => Promise<Record<string, any>>,
  maxAttempts = 3,
): Promise<Record<string, any>> {
  let inbox = await fetchInbox();
  if (!inbox.success || !Array.isArray(inbox.messages)) {
    return inbox.success
      ? { success: false, error: "Inbox update check returned malformed messages" }
      : inbox;
  }

  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    const newerMessages = inbox.messages as OutlookInboxMessage[];
    const latest = newerMessages[0];
    const sent = await fetchSent({
      account: p.account,
      conversation_id: p.conversation_id,
      received_after: latest?.received_at || p.received_after,
      sender_email: latest?.sender_email || p.sender_email,
    });
    if (!sent.success) return sent;

    const confirmed = await fetchInbox();
    if (!confirmed.success || !Array.isArray(confirmed.messages)) {
      return confirmed.success
        ? { success: false, error: "Inbox confirmation returned malformed messages" }
        : confirmed;
    }
    const confirmedMessages = confirmed.messages as OutlookInboxMessage[];
    if (inboxSnapshotKey(newerMessages) !== inboxSnapshotKey(confirmedMessages)) {
      inbox = confirmed;
      continue;
    }
    if (sent.found) {
      return {
        success: true,
        already_replied: true,
        sent_reply: sent,
        newer_messages: confirmedMessages,
      };
    }
    return {
      success: true,
      already_replied: false,
      newer_messages: confirmedMessages,
      latest_inbox_message: confirmedMessages[0] ?? null,
    };
  }
  return {
    success: false,
    error: `Inbox changed during ${maxAttempts} consecutive Sent Items checks; retry New Email Check`,
  };
}

/**
 * Read-only, fail-closed freshness check used by Mailroom approval cards.
 * A Sent Items reply suppresses the draft only when it is newer than the latest
 * related Inbox message; otherwise the newer Inbox message must be redrafted.
 */
export async function checkOutlookThreadUpdates(
  p: OutlookThreadUpdateInput,
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  try {
    const { connectionId } = resolveConnection(p.account);
    return await stabilizeOutlookThreadUpdates(
      p,
      () => findOutlookInboxUpdatesResolved(p, connectionId, signal),
      (guard) => findOutlookSentReplyResolved(guard, connectionId, signal),
    );
  } catch (error: any) {
    return { success: false, error: String(error?.message ?? error) };
  }
}

/** Programmatic service used by approval handlers; never sends. */
export async function createOutlookReplyDraft(
  p: OutlookReplyDraftInput,
  config: Cfg = {},
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  try {
    const { connectionId, email: ownerEmail } = resolveConnection(p.account);
    const initialGuard = await runReplyGuard(p, connectionId, signal);
    if (initialGuard) return initialGuard;
    const warnings: string[] = [];
    const mode = p.reply_all ?? "auto";
    let action = mode === "all" ? "createReplyAll" : "createReply";
    if (mode === "auto") {
      const orig = await maton(
        "GET", `/${enc(p.message_id)}?$select=toRecipients,ccRecipients`,
        connectionId, undefined, signal,
      );
      if (![200, 201].includes(orig.status)) {
        action = "createReplyAll";
        warnings.push(
          `could not fetch recipient count (HTTP ${orig.status}); defaulting to reply-all — confirm recipients before sending`,
        );
      } else {
        const others = [
          ...(orig.json?.toRecipients ?? []),
          ...(orig.json?.ccRecipients ?? []),
        ]
          .map((r: any) => (r?.emailAddress?.address ?? "").toLowerCase())
          .filter((a: string) => a && a !== ownerEmail.toLowerCase());
        if (others.length < 10) action = "createReplyAll";
        else {
          action = "createReply";
          warnings.push(
            `thread has ${others.length} recipients (≥10) — replying to sender only; confirm recipients before sending`,
          );
        }
      }
    }

    const finalGuard = await runReplyGuard(p, connectionId, signal);
    if (finalGuard) return finalGuard;
    const cr = await maton("POST", `/${enc(p.message_id)}/${action}`, connectionId, {}, signal);
    if (![200, 201].includes(cr.status)) {
      return { success: false, error: `${action} failed: HTTP ${cr.status} — ${cr.text.slice(0, 200)}` };
    }
    const replyId = cr.json?.id as string | undefined;
    if (!replyId) return { success: false, error: `${action} returned no draft id (HTTP ${cr.status})` };
    const bodyObj = cr.json.body ?? {};
    const ctype = String(bodyObj.contentType ?? "html").toLowerCase();
    const existing = String(bodyObj.content ?? "");
    if (!existing) warnings.push("quoted chain came back empty — recipient may not see thread context");

    const sigMode = p.signature ?? "auto";
    const useSignature = wantSignature(sigMode, true, [], config);
    const sig = useSignature ? loadSignature(p.account) : null;
    if (useSignature && !sig && sigMode === "on") {
      warnings.push(`no signature stored for ${p.account}; replied without one`);
    }
    let sigApplied = false;
    let patch: Record<string, unknown>;
    if (ctype === "html") {
      const newHtml = bodyToHtml(p.reply_html, p.reply);
      const sigHtml = sig ? `<br>${sig}` : "";
      if (sig) sigApplied = true;
      patch = {
        body: {
          contentType: "HTML",
          content: combineReplyHtml(`${FONT_DIV}${newHtml}</div>${sigHtml}`, existing),
        },
      };
    } else {
      if (sig) warnings.push("reply thread is plain-text; HTML signature not applied");
      patch = {
        body: {
          contentType: "Text",
          content: `${unescapeNewlines(p.reply ?? p.reply_html ?? "")}\n\n${existing}`,
        },
      };
    }

    const pr = await maton("PATCH", `/${enc(replyId)}`, connectionId, patch, signal);
    if (![200, 201].includes(pr.status)) {
      return { success: false, draft_id: replyId, error: `PATCH failed: HTTP ${pr.status}` };
    }

    if (p.attachments?.length) {
      const attachErr = await addAttachments(replyId, connectionId, p.attachments, signal);
      if (attachErr) {
        return {
          success: false,
          draft_id: replyId,
          error: attachErr,
          warning: `a partial reply draft was left in Outlook Drafts (draft_id ${replyId}) — delete it or fix the attachment and re-draft`,
        };
      }
    }

    const fp = await draftFingerprint(p.account, replyId, connectionId, signal);
    return {
      success: true,
      account: p.account,
      draft_id: replyId,
      approval_token: fp.ok ? fp.token : undefined,
      reply_mode: action,
      signature_applied: sigApplied,
      warnings,
    };
  } catch (e: any) {
    return { success: false, error: String(e?.message ?? e) };
  }
}

/** Remove a Mailroom-created Outlook draft that is being superseded or abandoned. */
export async function deleteOutlookDraft(
  p: { account: string; draft_id: string },
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  try {
    const { connectionId } = resolveConnection(p.account);
    const result = await maton("DELETE", `/${enc(p.draft_id)}`, connectionId, undefined, signal);
    if (result.status === 404) {
      return {
        success: true, deleted: false, already_absent: true,
        account: p.account, draft_id: p.draft_id,
      };
    }
    if (![200, 202, 204].includes(result.status)) {
      return {
        success: false,
        error: `could not delete superseded Outlook draft (HTTP ${result.status}) — ${result.text.slice(0, 200)}`,
      };
    }
    return {
      success: true, deleted: true,
      account: p.account, draft_id: p.draft_id, status: result.status,
    };
  } catch (error: any) {
    return { success: false, error: String(error?.message ?? error) };
  }
}

/** Programmatic fingerprint-gated send used only after the operator's second approval. */
export async function sendOutlookDraft(
  p: OutlookSendInput,
  signal?: AbortSignal,
): Promise<Record<string, any>> {
  let sendAttempted = false;
  try {
    const { connectionId } = resolveConnection(p.account);
    const initialGuard = await runReplyGuard(p, connectionId, signal);
    if (initialGuard) return { ...initialGuard, send_attempted: false };
    const fp = await draftFingerprint(p.account, p.draft_id, connectionId, signal);
    if (!fp.ok) {
      return {
        success: false, send_attempted: false,
        error: `could not verify draft before send (HTTP ${fp.status}) — NOT sent`,
      };
    }
    if (fp.token !== p.approval_token) {
      return {
        success: false, send_attempted: false,
        error: "approval_token does not match the draft's current content — NOT sent",
      };
    }
    const finalGuard = await runReplyGuard(p, connectionId, signal);
    if (finalGuard) return { ...finalGuard, send_attempted: false };
    sendAttempted = true;
    const r = await maton("POST", `/${enc(p.draft_id)}/send`, connectionId, {}, signal);
    if (![200, 202, 204].includes(r.status)) {
      return {
        success: false, send_attempted: true,
        error: `send failed: HTTP ${r.status} — ${r.text.slice(0, 200)}`,
      };
    }
    return {
      success: true, send_attempted: true,
      account: p.account, draft_id: p.draft_id, status: r.status,
    };
  } catch (e: any) {
    return { success: false, send_attempted: sendAttempted, error: String(e?.message ?? e) };
  }
}
