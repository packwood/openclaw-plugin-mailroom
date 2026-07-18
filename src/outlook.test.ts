import { describe, expect, it } from "vitest";
import {
  normalizeGraphUrl,
  selectNewerInboxMessages,
  selectSentReply,
  stabilizeOutlookThreadUpdates,
} from "./outlook.js";

describe("Mailroom Outlook adapter", () => {
  it("selects only a non-draft sent reply after the inbound message", () => {
    const result = selectSentReply([
      { id: "old", conversationId: "thread", sentDateTime: "2026-07-12T11:00:00Z", isDraft: false },
      { id: "draft", conversationId: "thread", sentDateTime: "2026-07-12T13:00:00Z", isDraft: true },
      { id: "forward", conversationId: "thread", sentDateTime: "2026-07-12T13:30:00Z", isDraft: false, toRecipients: [{ emailAddress: { address: "other@example.com" } }] },
      { id: "reply", conversationId: "thread", sentDateTime: "2026-07-12T12:30:00Z", isDraft: false, toRecipients: [{ emailAddress: { address: "sender@example.com" } }] },
    ], "thread", "2026-07-12T12:00:00Z", "sender@example.com");
    expect(result).toMatchObject({ message_id: "reply", sent_at: "2026-07-12T12:30:00Z" });
  });

  it("fails closed when a successful Sent Items response has no value array", () => {
    expect(() => selectSentReply({}, "thread", "2026-07-12T12:00:00Z", "sender@example.com"))
      .toThrow("message value array");
  });

  it("selects, normalizes, and orders only newer messages in the exact Inbox conversation", () => {
    const result = selectNewerInboxMessages([
      { id: "old", conversationId: "thread", receivedDateTime: "2026-07-12T11:00:00Z", from: { emailAddress: { address: "old@example.com" } } },
      { id: "other", conversationId: "other-thread", receivedDateTime: "2026-07-12T15:00:00Z", from: { emailAddress: { address: "other@example.com" } } },
      { id: "new-1", conversationId: "thread", receivedDateTime: "2026-07-12T13:00:00Z", subject: "Re: Project", from: { emailAddress: { name: "Alice", address: "alice@example.com" } }, toRecipients: [{ emailAddress: { name: "Operator", address: "operator@example.com" } }], body: { contentType: "html", content: "<p>First update</p>" }, bodyPreview: "First update", hasAttachments: true },
      { id: "new-2", conversationId: "thread", receivedDateTime: "2026-07-12T14:00:00Z", from: { emailAddress: { address: "bob@example.com" } }, bodyPreview: "Latest update" },
    ], "thread", "2026-07-12T12:00:00Z");
    expect(result.map((message) => message.message_id)).toEqual(["new-2", "new-1"]);
    expect(result[1]).toMatchObject({
      sender_email: "alice@example.com", sender_name: "Alice",
      body_content: "<p>First update</p>", has_attachments: true,
      to_recipients: [{ name: "Operator", address: "operator@example.com" }],
    });
  });

  it("fails closed when a successful Inbox response has no value array", () => {
    expect(() => selectNewerInboxMessages({}, "thread", "2026-07-12T12:00:00Z"))
      .toThrow("message value array");
  });

  it("normalizes trusted Graph continuation URLs and rejects untrusted hosts", () => {
    expect(normalizeGraphUrl("https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages?$skip=10"))
      .toBe("https://gateway.maton.ai/outlook/v1.0/me/mailFolders/sentitems/messages?$skip=10");
    expect(() => normalizeGraphUrl("https://evil.example/messages?$skip=10"))
      .toThrow("Untrusted Sent Items continuation host");
  });

  it("rechecks Sent Items when Inbox changes during the cross-folder check", async () => {
    const first = {
      message_id: "new-1", internet_message_id: null, conversation_id: "thread",
      received_at: "2026-07-12T13:00:00Z", subject: null,
      sender_email: "sender@example.com", sender_name: null,
      to_recipients: [], cc_recipients: [], body_preview: "first", body_content: "",
      body_content_type: null, has_attachments: false, attachments: [],
    };
    const second = { ...first, message_id: "new-2", received_at: "2026-07-12T14:00:00Z" };
    const inboxResults = [
      { success: true, messages: [first] },
      { success: true, messages: [second, first] },
      { success: true, messages: [second, first] },
    ];
    const cutoffs: string[] = [];
    const result = await stabilizeOutlookThreadUpdates({
      account: "work", conversation_id: "thread",
      received_after: "2026-07-12T12:00:00Z", sender_email: "sender@example.com",
    }, async () => inboxResults.shift()!, async (guard) => {
      cutoffs.push(guard.received_after);
      return cutoffs.length === 1
        ? { success: true, found: true, message_id: "sent-1" }
        : { success: true, found: false };
    });
    expect(cutoffs).toEqual(["2026-07-12T13:00:00Z", "2026-07-12T14:00:00Z"]);
    expect(result).toMatchObject({
      success: true, already_replied: false,
      latest_inbox_message: { message_id: "new-2" },
    });
  });
});
