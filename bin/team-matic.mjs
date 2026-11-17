#!/usr/bin/env node
// npx launcher: runs teammatic.py with the local Python. State goes to a stable user folder,
// because the npx package folder is a cache that npm may replace or delete.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const script = join(root, "teammatic.py");
const env = { ...process.env };
if (!env.TEAMMATIC_DATA && !existsSync(join(root, ".git"))) {
  env.TEAMMATIC_DATA = join(env.LOCALAPPDATA || join(homedir(), "AppData", "Local"), "team-matic");
}

const candidates = [["python", []], ["py", ["-3"]], ["python3", []]];
for (const [exe, pre] of candidates) {
  const r = spawnSync(exe, [...pre, script, ...process.argv.slice(2)], { stdio: "inherit", env });
  if (r.error && r.error.code === "ENOENT") continue;
  process.exit(r.status ?? 1);
}
console.error(JSON.stringify({ ok: false, error: "Python 3.10+ not found. Install it from python.org and retry." }));
process.exit(1);
