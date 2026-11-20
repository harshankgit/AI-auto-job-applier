import fs from "node:fs";
import path from "node:path";

/**
 * Every path used by the dashboard is resolved from the Python project root,
 * never from a relative guess, so the app behaves the same under `next dev`,
 * `next start` and a standalone server.
 *
 * Resolution order:
 *   1. JOBBOT_ROOT env var (explicit override, e.g. for deployment)
 *   2. walk up from cwd looking for the bot's own marker files
 *   3. parent of cwd (the dashboard lives at <root>/web)
 */
const MARKERS = ["config/profiles.yaml", "jobbot/__init__.py"] as const;

function looksLikeRoot(dir: string): boolean {
  try {
    return MARKERS.every((marker) => fs.existsSync(path.join(dir, marker)));
  } catch {
    return false;
  }
}

function discoverRoot(): string {
  const override = process.env.JOBBOT_ROOT?.trim();
  if (override) return path.resolve(override);

  let dir = process.cwd();
  for (let depth = 0; depth < 6; depth += 1) {
    if (looksLikeRoot(dir)) return dir;
    const parent = path.dirname(dir);
    if (parent === dir) break;
    dir = parent;
  }
  return path.resolve(process.cwd(), "..");
}

export const PROJECT_ROOT = discoverRoot();

/**
 * Read-only copy of the reports bundled at build time (scripts/snapshot.mjs).
 * Used when the bot's project root is absent, e.g. on Vercel, so the hosted
 * dashboard still shows the latest pushed applications instead of crashing.
 */
export const SNAPSHOT_DIR = path.join(process.cwd(), "snapshot");
const HAS_ROOT = looksLikeRoot(PROJECT_ROOT);

function pick(live: string, snapshotName: string): string {
  if (HAS_ROOT) return live;
  const snap = path.join(SNAPSHOT_DIR, snapshotName);
  return fs.existsSync(snap) ? snap : live;
}

export const PATHS = {
  root: PROJECT_ROOT,
  db: path.join(PROJECT_ROOT, "data", "applications.db"),
  csv: pick(path.join(PROJECT_ROOT, "reports", "applications.csv"), "applications.csv"),
  markdown: pick(path.join(PROJECT_ROOT, "reports", "APPLICATIONS.md"), "APPLICATIONS.md"),
  log: path.join(PROJECT_ROOT, "logs", "realrun.log"),
  logDir: path.join(PROJECT_ROOT, "logs"),
  profiles: pick(path.join(PROJECT_ROOT, "config", "profiles.yaml"), "profiles.yaml"),
  resumeUploads: path.join(PROJECT_ROOT, "data", "resumes"),
  chromeProfile: path.join(PROJECT_ROOT, "data", "chrome-profile"),
} as const;

/** True when the project root actually exists on this machine (false on Vercel). */
export function rootIsPresent(): boolean {
  return looksLikeRoot(PROJECT_ROOT);
}
