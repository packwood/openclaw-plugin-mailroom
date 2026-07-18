import { readdirSync, rmSync } from "node:fs";
import { basename, resolve } from "node:path";

const target = resolve(process.cwd(), "dist");
if (target === resolve(process.cwd()) || basename(target) !== "dist") {
  throw new Error(`Refusing to clean unexpected path: ${target}`);
}
rmSync(target, { recursive: true, force: true });

function removePythonCaches(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "__pycache__") {
        rmSync(path, { recursive: true, force: true });
      } else {
        removePythonCaches(path);
      }
    } else if (entry.isFile() && entry.name.endsWith(".pyc")) {
      rmSync(path, { force: true });
    }
  }
}

removePythonCaches(resolve(process.cwd(), "python"));
