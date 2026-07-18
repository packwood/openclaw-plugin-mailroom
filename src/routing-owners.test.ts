import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  publishRoutingOwnerPolicy,
  readProfileAgentIds,
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
