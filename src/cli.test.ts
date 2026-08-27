import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { buildEngineArgs, withConfiguredProfilesDir } from "./cli.js";

describe("Mailroom profile CLI forwarding", () => {
  it("uses the directory containing the configured current profile", () => {
    const root = join("", "tmp", "custom-mailroom-profiles");
    expect(withConfiguredProfilesDir(join(root, "current.json"), ["generate"]))
      .toEqual(["generate", "--profiles-dir", root]);
  });

  it("preserves an explicit profiles directory", () => {
    expect(withConfiguredProfilesDir(
      "/configured/current.json",
      ["show", "--profiles-dir", "/explicit"],
    )).toEqual(["show", "--profiles-dir", "/explicit"]);
  });

  it("rejects configured profile filenames the Python store cannot address", () => {
    expect(() => withConfiguredProfilesDir("/profiles/custom.json", ["show"]))
      .toThrow("must end with current.json");
  });
});

describe("Mailroom engine CLI forwarding", () => {
  it("uses configured state, profiles, account aliases, and approval chat", () => {
    expect(buildEngineArgs({
      dbPath: "/state/mailroom.db",
      profilesPath: "/state/profiles/current.json",
      telegramChatId: "private-chat",
      telegramThreadId: "12",
      telegramDestinations: {
        billy: { chatId: "-1000000000001", threadId: "21" },
      },
      routingReviewAgentId: "main",
      routingReviewTelegramAccountId: "default",
      accounts: { work: "operator@example.com" },
    }, "cycle", ["--account", "work"])).toEqual([
      "--db", "/state/mailroom.db", "cycle",
      "--account", "operator@example.com",
      "--profiles-dir", "/state/profiles",
      "--telegram-chat-id", "private-chat",
      "--telegram-thread-id", "12",
      "--telegram-destinations", JSON.stringify({
        billy: { chatId: "-1000000000001", threadId: "21" },
      }),
      "--routing-review-agent-id", "main",
      "--routing-review-telegram-account-id", "default",
    ]);
  });

  it("preserves explicit operational overrides", () => {
    expect(buildEngineArgs({
      telegramChatId: "configured-chat",
      telegramThreadId: "12",
      telegramDestinations: { billy: { chatId: "-1000000000001" } },
      accounts: { work: "operator@example.com" },
    }, "dispatch", [
      "--account=other@example.com", "--telegram-chat-id", "explicit-chat",
      "--telegram-thread-id", "99",
      "--telegram-destinations", "{\"recon\":{\"chatId\":\"-1000000000002\"}}",
      "--routing-review-agent-id=coordinator",
      "--routing-review-telegram-account-id", "orchestrator",
      "--profiles-dir=/explicit",
    ])).toEqual([
      "dispatch", "--account=other@example.com",
      "--telegram-chat-id", "explicit-chat",
      "--telegram-thread-id", "99",
      "--telegram-destinations", "{\"recon\":{\"chatId\":\"-1000000000002\"}}",
      "--routing-review-agent-id=coordinator",
      "--routing-review-telegram-account-id", "orchestrator",
      "--profiles-dir=/explicit",
    ]);
  });

  it("requires a configured approval chat for production commands", () => {
    expect(() => buildEngineArgs({}, "cycle", ["--account", "work"]))
      .toThrow("telegramChatId");
  });
});
