#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const { join } = require("node:path");

const root = process.cwd();
const isWindows = process.platform === "win32";

const command = isWindows ? "powershell.exe" : "bash";
const args = isWindows
  ? ["-ExecutionPolicy", "Bypass", "-File", join(root, "sync.ps1")]
  : [join(root, "sync.sh")];

const result = spawnSync(command, args, { stdio: "inherit" });

if (result.error) {
  console.error(result.error.message);
  process.exit(1);
}

process.exit(result.status ?? 0);
