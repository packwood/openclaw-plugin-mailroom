import { chmodSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  publishRoutingOwnerPolicy,
  discoverOpenClawAgentIds,
  readProfileAgentIds,
  RoutingOwnerDiscoveryError,
  resolveRoutingOwnerIds,
} from "./routing-owners.js";

const roots: string[] = [];
function temporaryRoot(): string {
  const root = mkdtempSync(join(tmpdir(), "mailroom-routing-"));
  roots.push(root);
  return root;
}

afterEach(() => {
  for (const root of roots.splice(0)) rmSync(root, { recursive: true, force: true });
});

describe("routing owners", () => {
  it("uses every agent in a valid responsibility-profile set", async () => {
    const path = join(temporaryRoot(), "current.json");
    writeFileSync(path, JSON.stringify({
      validation_status: "valid",
      fleet_agent_ids: ["research", "primary"],
      profiles: [{ agent_id: "primary" }, { agent_id: "research" }],
    }));
    expect(readProfileAgentIds(path)).toEqual(["primary", "research"]);
    await expect(resolveRoutingOwnerIds({
      profilesPath: path,
      agentDiscovery: async () => ["research", "primary"],
    })).resolves.toEqual([
      "primary", "research",
    ]);
  });

  it("fails closed when profiles omit a newly discovered agent", async () => {
    const path = join(temporaryRoot(), "current.json");
    writeFileSync(path, JSON.stringify({
      validation_status: "valid",
      fleet_agent_ids: ["primary"],
      profiles: [{ agent_id: "primary" }],
    }));
    await expect(resolveRoutingOwnerIds({
      profilesPath: path,
      agentDiscovery: async () => ["primary", "falcon"],
    })).rejects.toThrow("missing profiles: falcon");
  });

  it("falls back to OpenClaw agent discovery when profiles do not exist", async () => {
    await expect(resolveRoutingOwnerIds({
      profilesPath: join(temporaryRoot(), "missing.json"),
      agentDiscovery: async () => ["bravo", "alpha", "bravo"],
    })).resolves.toEqual(["alpha", "bravo"]);
  });

  it("discovers a fleet listing larger than the former 1 MiB stdout cap", async () => {
    const root = temporaryRoot();
    const executable = join(root, "openclaw");
    const originalPath = process.env.PATH;
    const oversizedAvatar = `data:image/png;base64,${"a".repeat(1_300_000)}`;
    writeFileSync(executable, `#!/usr/bin/env node\nprocess.stdout.write(${JSON.stringify(JSON.stringify({
      agents: [{ id: "primary", identityAvatarUrl: oversizedAvatar }, { id: "research" }],
    }))});\n`);
    chmodSync(executable, 0o755);
    process.env.PATH = `${root}:${originalPath}`;
    try {
      await expect(discoverOpenClawAgentIds()).resolves.toEqual(["primary", "research"]);
    } finally {
      process.env.PATH = originalPath;
    }
  });

  it("fails closed with a discovery error for an unparseable CLI response", async () => {
    const root = temporaryRoot();
    const executable = join(root, "openclaw");
    const originalPath = process.env.PATH;
    writeFileSync(executable, "#!/bin/sh\nprintf '{truncated'\n");
    chmodSync(executable, 0o755);
    process.env.PATH = `${root}:${originalPath}`;
    try {
      await expect(discoverOpenClawAgentIds()).rejects.toBeInstanceOf(RoutingOwnerDiscoveryError);
      await expect(discoverOpenClawAgentIds()).rejects.toThrow("unparseable JSON");
    } finally {
      process.env.PATH = originalPath;
    }
  });

  it("honors selected mode", async () => {
    await expect(resolveRoutingOwnerIds({
      routingOwnerMode: "selected", reviewOwners: ["bravo", "alpha", "bravo"],
    })).resolves.toEqual(["alpha", "bravo"]);
  });

  it("rejects profile sets that do not exactly cover their fleet", () => {
    const path = join(temporaryRoot(), "current.json");
    writeFileSync(path, JSON.stringify({
      validation_status: "valid",
      fleet_agent_ids: ["primary", "research"],
      profiles: [{ agent_id: "primary" }],
    }));
    expect(() => readProfileAgentIds(path)).toThrow("do not exactly match");
  });

  it("publishes the cross-runtime policy atomically", () => {
    const path = join(temporaryRoot(), "routing-owner-policy.json");
    publishRoutingOwnerPolicy({
      routingOwnerMode: "selected", reviewOwners: ["bravo", "alpha"],
    }, path);
    expect(JSON.parse(readFileSync(path, "utf8"))).toEqual({
      schema_version: 1,
      mode: "selected",
      selected_agent_ids: ["alpha", "bravo"],
    });
  });
});
