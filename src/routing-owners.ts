import { execFile } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const AGENT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

export type RoutingOwnerMode = "all" | "selected";

export type RoutingOwnerConfig = {
  routingOwnerMode?: RoutingOwnerMode;
  reviewOwners?: string[];
  profilesPath?: string;
  agentDiscovery?: () => Promise<string[]>;
};

export const DEFAULT_PROFILES_PATH = join(
  homedir(), ".openclaw", "mailroom", "responsibility-profiles", "current.json",
);

export const DEFAULT_POLICY_PATH = join(
  homedir(), ".openclaw", "mailroom", "routing-owner-policy.json",
);

export function normalizeAgentIds(values: unknown): string[] {
  if (!Array.isArray(values)) return [];
  return [...new Set(values.map(String).map((value) => value.trim()).filter(
    (value) => AGENT_ID.test(value),
  ))].sort();
}

export function readProfileAgentIds(path = DEFAULT_PROFILES_PATH): string[] {
  if (!existsSync(path)) return [];
  const value = JSON.parse(readFileSync(path, "utf8"));
  if (
    value?.validation_status !== "valid"
    || !Array.isArray(value?.fleet_agent_ids)
    || !Array.isArray(value?.profiles)
  ) {
    throw new Error("Mailroom responsibility profile set is invalid");
  }
  const declared = normalizeAgentIds(value.fleet_agent_ids);
  const profiled = normalizeAgentIds(value.profiles.map((profile: any) => profile?.agent_id));
  if (!declared.length || JSON.stringify(declared) !== JSON.stringify(profiled)) {
    throw new Error("Mailroom responsibility profiles do not exactly match their fleet IDs");
  }
  return declared;
}

export async function discoverOpenClawAgentIds(): Promise<string[]> {
  const { stdout } = await execFileAsync("openclaw", ["agents", "list", "--json"], {
    timeout: 60000,
    maxBuffer: 1024 * 1024,
  });
  const value = JSON.parse(stdout);
  const agents = Array.isArray(value) ? value : value?.agents;
  if (!Array.isArray(agents)) throw new Error("OpenClaw agent discovery returned no agent list");
  return normalizeAgentIds(agents.map((agent: any) => agent?.id ?? agent?.agentId));
}

export async function resolveRoutingOwnerIds(cfg: RoutingOwnerConfig): Promise<string[]> {
  if ((cfg.routingOwnerMode ?? "all") === "selected") {
    return normalizeAgentIds(cfg.reviewOwners);
  }
  const profiled = readProfileAgentIds(cfg.profilesPath || DEFAULT_PROFILES_PATH);
  const discovered = normalizeAgentIds(
    await (cfg.agentDiscovery ?? discoverOpenClawAgentIds)(),
  );
  if (!profiled.length) return discovered;
  if (JSON.stringify(profiled) !== JSON.stringify(discovered)) {
    const missing = discovered.filter((agentId) => !profiled.includes(agentId));
    const stale = profiled.filter((agentId) => !discovered.includes(agentId));
    throw new Error(
      "Mailroom responsibility profiles do not match the discovered OpenClaw fleet"
      + `${missing.length ? `; missing profiles: ${missing.join(", ")}` : ""}`
      + `${stale.length ? `; stale profiles: ${stale.join(", ")}` : ""}`,
    );
  }
  return profiled;
}

export function publishRoutingOwnerPolicy(
  cfg: RoutingOwnerConfig,
  path = DEFAULT_POLICY_PATH,
): void {
  const mode = cfg.routingOwnerMode ?? "all";
  const selected = mode === "selected" ? normalizeAgentIds(cfg.reviewOwners) : [];
  if (mode === "selected" && !selected.length) {
    throw new Error("Mailroom selected routing-owner mode requires at least one review owner");
  }
  const root = dirname(path);
  mkdirSync(root, { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.${Date.now()}.tmp`;
  try {
    writeFileSync(temporary, `${JSON.stringify({
      schema_version: 1,
      mode,
      selected_agent_ids: selected,
    }, null, 2)}\n`, { encoding: "utf8", mode: 0o600, flag: "wx" });
    renameSync(temporary, path);
  } finally {
    try { unlinkSync(temporary); } catch { /* already renamed or never created */ }
  }
}
