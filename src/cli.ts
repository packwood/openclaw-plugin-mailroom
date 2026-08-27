import { execFileSync } from "node:child_process";
import { basename, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import {
  discoverOpenClawAgentIds,
  readProfileAgentIds,
  resolveRoutingOwnerIds,
  type RoutingOwnerConfig,
} from "./routing-owners.js";

type CliContext = {
  program: any;
  cfg: RoutingOwnerConfig & {
    dbPath?: string;
    pythonExecutable?: string;
    pythonPath?: string;
    telegramChatId?: string;
    telegramThreadId?: string;
    telegramDestinations?: Record<string, { chatId: string; threadId?: string }>;
    routingReviewAgentId?: string;
    routingReviewTelegramAccountId?: string;
    accounts?: Record<string, string>;
  };
};

function parseOwners(value: string): string[] {
  return [...new Set(value.split(",").map((owner) => owner.trim()).filter(Boolean))];
}

function setConfig(key: string, value: unknown): void {
  execFileSync("openclaw", [
    "config", "set", `plugins.entries.mailroom.config.${key}`,
    JSON.stringify(value), "--strict-json",
  ], { stdio: "inherit" });
}

function generateProfiles(cfg: CliContext["cfg"]): void {
  runProfiles(cfg, ["generate"]);
}

function hasOption(args: string[], name: string): boolean {
  return args.some((argument) => argument === name || argument.startsWith(`${name}=`));
}

function replaceOptionValue(
  args: string[], name: string, resolve: (value: string) => string,
): string[] {
  const result = [...args];
  for (let index = 0; index < result.length; index += 1) {
    if (result[index] === name && index + 1 < result.length) {
      result[index + 1] = resolve(result[index + 1]);
      index += 1;
    } else if (result[index].startsWith(`${name}=`)) {
      result[index] = `${name}=${resolve(result[index].slice(name.length + 1))}`;
    }
  }
  return result;
}

export function buildEngineArgs(
  cfg: CliContext["cfg"], command: string, args: string[],
): string[] {
  let forwarded = replaceOptionValue(args, "--account", (value) => (
    cfg.accounts?.[value] || value
  ));
  if (["shadow", "cycle", "dispatch"].includes(command)) {
    forwarded = withConfiguredProfilesDir(cfg.profilesPath, forwarded);
  }
  if (["cycle", "dispatch"].includes(command) && !hasOption(forwarded, "--telegram-chat-id")) {
    if (!cfg.telegramChatId) {
      throw new Error("Mailroom telegramChatId must be configured for this command");
    }
    forwarded.push("--telegram-chat-id", cfg.telegramChatId);
  }
  if (
    ["cycle", "dispatch"].includes(command)
    && cfg.telegramThreadId
    && !hasOption(forwarded, "--telegram-thread-id")
  ) {
    forwarded.push("--telegram-thread-id", cfg.telegramThreadId);
  }
  if (
    ["cycle", "dispatch"].includes(command)
    && cfg.telegramDestinations
    && Object.keys(cfg.telegramDestinations).length
    && !hasOption(forwarded, "--telegram-destinations")
  ) {
    forwarded.push("--telegram-destinations", JSON.stringify(cfg.telegramDestinations));
  }
  if (
    ["cycle", "dispatch"].includes(command)
    && !hasOption(forwarded, "--routing-review-agent-id")
  ) {
    forwarded.push(
      "--routing-review-agent-id",
      cfg.routingReviewAgentId || "main",
    );
  }
  if (
    ["cycle", "dispatch"].includes(command)
    && !hasOption(forwarded, "--routing-review-telegram-account-id")
  ) {
    forwarded.push(
      "--routing-review-telegram-account-id",
      cfg.routingReviewTelegramAccountId || "default",
    );
  }
  return [
    ...(cfg.dbPath ? ["--db", cfg.dbPath] : []),
    command,
    ...forwarded,
  ];
}

function runEngine(cfg: CliContext["cfg"], command: string, args: string[]): void {
  const pythonPath = cfg.pythonPath
    || fileURLToPath(new URL("../python", import.meta.url));
  execFileSync(cfg.pythonExecutable || "python3", [
    "-m", "mailroom.cli", ...buildEngineArgs(cfg, command, args),
  ], {
    stdio: "inherit",
    env: { ...process.env, PYTHONPATH: pythonPath },
  });
}

export function withConfiguredProfilesDir(
  profilesPath: string | undefined,
  args: string[],
): string[] {
  const forwarded = [...args];
  if (!profilesPath || forwarded.some(
    (argument) => argument === "--profiles-dir" || argument.startsWith("--profiles-dir="),
  )) return forwarded;
  if (basename(profilesPath) !== "current.json") {
    throw new Error("Mailroom profilesPath must end with current.json");
  }
  forwarded.push("--profiles-dir", dirname(profilesPath));
  return forwarded;
}

function runProfiles(cfg: CliContext["cfg"], args: string[]): void {
  const pythonPath = cfg.pythonPath
    || fileURLToPath(new URL("../python", import.meta.url));
  const forwarded = withConfiguredProfilesDir(cfg.profilesPath, args);
  execFileSync(cfg.pythonExecutable || "python3", [
    "-m", "mailroom.cli", "profiles", ...forwarded,
  ], {
    stdio: "inherit",
    env: { ...process.env, PYTHONPATH: pythonPath },
  });
}

export function registerMailroomCli({ program, cfg }: CliContext): void {
  const mailroom = program
    .command("mailroom")
    .description("Configure Mailroom routing owners and responsibility profiles");

  mailroom.command("routing-status")
    .description("Show discovered, profiled, and currently routable OpenClaw agents")
    .action(async () => {
      const discovered = await discoverOpenClawAgentIds();
      const profiled = readProfileAgentIds(cfg.profilesPath);
      const missingProfiles = discovered.filter((agentId) => !profiled.includes(agentId));
      const staleProfiles = profiled.filter((agentId) => !discovered.includes(agentId));
      let routable: string[] = [];
      let routingError: string | null = null;
      try {
        routable = await resolveRoutingOwnerIds(cfg);
      } catch (error: any) {
        routingError = String(error?.message ?? error);
      }
      const ok = !routingError && !missingProfiles.length && !staleProfiles.length;
      process.stdout.write(`${JSON.stringify({
        ok,
        mode: cfg.routingOwnerMode ?? "all",
        discovered_agent_ids: discovered,
        profiled_agent_ids: profiled,
        routable_agent_ids: routable,
        missing_profile_agent_ids: missingProfiles,
        stale_profile_agent_ids: staleProfiles,
        error: routingError,
      }, null, 2)}\n`);
      if (!ok) process.exitCode = 2;
    });

  mailroom.command("setup-routing")
    .description("Make all discovered agents routable, or select a comma-separated subset")
    .option("--all", "Make every discovered/profiled agent a routing owner")
    .option("--owners <agent-ids>", "Only make these comma-separated agent IDs routable")
    .option("--generate-profiles", "Generate and validate responsibility profiles first")
    .option("--restart", "Restart the OpenClaw gateway after updating configuration")
    .action(async (options: any) => {
      if (options.all && options.owners) {
        throw new Error("Choose either --all or --owners, not both");
      }
      if (options.generateProfiles) generateProfiles(cfg);
      const discovered = await discoverOpenClawAgentIds();
      const profiled = readProfileAgentIds(cfg.profilesPath);
      const missingProfiles = discovered.filter((agentId) => !profiled.includes(agentId));
      if (options.generateProfiles && missingProfiles.length) {
        throw new Error(
          `Generated responsibility profiles still omit: ${missingProfiles.join(", ")}`,
        );
      }
      const selected = options.owners ? parseOwners(options.owners) : discovered;
      const unknown = selected.filter((owner) => !discovered.includes(owner));
      if (!selected.length) throw new Error("No routing owners were selected");
      if (unknown.length) {
        throw new Error(`Unknown OpenClaw agent ID(s): ${unknown.join(", ")}`);
      }
      const mode = options.owners ? "selected" : "all";
      setConfig("reviewOwners", selected);
      setConfig("routingOwnerMode", mode);
      process.stdout.write(
        `Mailroom routing owners configured (${mode}): ${selected.join(", ")}\n`,
      );
      if (options.restart) {
        execFileSync("openclaw", ["gateway", "restart"], { stdio: "inherit" });
      } else {
        process.stdout.write("Restart the gateway to activate the new policy.\n");
      }
    });

  mailroom.command("profiles")
    .description("Show, edit, validate, generate, and diff Agent Responsibility Profiles")
    .allowUnknownOption(true)
    .argument("[args...]", "Arguments passed to mailroom profiles")
    .action((args: string[]) => {
      runProfiles(cfg, args.length ? args : ["show", "--routing-only"]);
    });

  const engineCommands: Array<[string, string]> = [
    ["init", "Initialize or migrate the configured Mailroom ledger"],
    ["shadow", "Run read-only intake and routing without drafts or alerts"],
    ["cycle", "Run one production intake, draft, approval, and reconciliation cycle"],
    ["dispatch", "Retry persisted drafting and approval-card delivery"],
    ["catchup", "Run historical routing without moving the live checkpoint"],
    ["reconcile", "Reconcile accepted or uncertain sends against Outlook Sent Items"],
    ["list", "List Mailroom ledger items using the bundled engine"],
  ];
  for (const [name, description] of engineCommands) {
    mailroom.command(name)
      .description(description)
      .allowUnknownOption(true)
      .argument("[args...]", `Arguments passed to mailroom ${name}`)
      .action((args: string[]) => runEngine(cfg, name, args || []));
  }
}
