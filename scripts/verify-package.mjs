import { execFileSync } from "node:child_process";

const raw = execFileSync(
  "npm",
  ["pack", "--dry-run", "--ignore-scripts", "--json"],
  { encoding: "utf8" },
);
const result = JSON.parse(raw)[0];
const files = new Set(result.files.map((entry) => entry.path));
const required = [
  "dist/index.js",
  "dist/outlook.js",
  "python/mailroom/cli.py",
  "python/mailroom/ledger.py",
  "openclaw.plugin.json",
  "README.md",
  "ARCHITECTURE.md",
  "LICENSE",
];
const missing = required.filter((path) => !files.has(path));
if (missing.length) {
  throw new Error(`Package is missing required files: ${missing.join(", ")}`);
}
if ([...files].some((path) => path.includes("__pycache__") || path.endsWith(".pyc"))) {
  throw new Error("Package contains Python cache files");
}
if ([...files].some((path) => path.includes(".test."))) {
  throw new Error("Package contains stale compiled test artifacts");
}
const privatePatterns = [
  [/^signatures\//, "operator signature files"],
  [/\.(db|sqlite3?|sqlite-wal|sqlite-shm)$/, "SQLite databases"],
  [/(^|\/)\.env(\.|$)/, "environment files"],
  [/^python\/mailroom\/rulepacks\/.*\.json$/, "private rulepacks"],
  [/(^|\/)connections\.json$/, "connection registries"],
];
for (const [pattern, label] of privatePatterns) {
  const hits = [...files].filter((path) => pattern.test(path));
  if (hits.length) {
    throw new Error(`Package contains ${label}: ${hits.join(", ")}`);
  }
}
console.log(`Verified ${result.filename}: ${files.size} files, ${result.unpackedSize} bytes`);
