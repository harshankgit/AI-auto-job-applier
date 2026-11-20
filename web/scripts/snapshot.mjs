// Copies the bot's latest reports into web/snapshot so a hosted build
// (Vercel, Root Directory = web) can serve them read-only. Missing files are
// skipped, never fatal: the dashboard falls back to its empty states.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const webDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.resolve(webDir, "..");
const out = path.join(webDir, "snapshot");
fs.mkdirSync(out, { recursive: true });

const files = [
  ["reports/applications.csv", "applications.csv"],
  ["reports/APPLICATIONS.md", "APPLICATIONS.md"],
  ["reports/failures.json", "failures.json"],
  ["config/profiles.yaml", "profiles.yaml"],
];
for (const [src, dst] of files) {
  const from = path.join(root, src);
  if (fs.existsSync(from)) {
    fs.copyFileSync(from, path.join(out, dst));
    console.log(`snapshot: ${src} -> snapshot/${dst}`);
  } else {
    console.log(`snapshot: ${src} not found, skipped`);
  }
}
