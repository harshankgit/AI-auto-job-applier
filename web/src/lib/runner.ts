import { type ChildProcess, execFile, spawn } from "node:child_process";
import fsp from "node:fs/promises";
import type { FileHandle } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

import { candidateLabel, type RunTarget } from "./candidates";
import { PATHS } from "./paths";
import type { LastRunInfo, RunStatus } from "./types";

const execFileAsync = promisify(execFile);

const RING_LIMIT = 500;
const POLL_MS = 600;
const MAX_CHUNK = 256 * 1024;
const EXTERNAL_TTL_MS = 2_500;

export type LineListener = (lines: string[]) => void;
export type EndListener = (status: RunStatus) => void;

interface Registry {
  child: ChildProcess | null;
  pid: number | null;
  startedAt: string | null;
  candidate: RunTarget | null;
  lastRun: LastRunInfo | null;
  ring: string[];
  lineSubs: Set<LineListener>;
  endSubs: Set<EndListener>;
  tailTimer: NodeJS.Timeout | null;
  tailOffset: number;
  tailPartial: string;
  tailBusy: boolean;
  seeded: boolean;
  lastActivityAt: number;
  externalPid: number | null;
  externalAt: number;
}

/**
 * Run state is parked on globalThis so it survives Next's dev-mode module
 * reloading — otherwise a hot reload would forget an in-flight Chrome run and
 * happily start a second one against the same persistent profile.
 */
const KEY = "__JOBBOT_DASHBOARD_RUNTIME__";

function registry(): Registry {
  const store = globalThis as unknown as Record<string, Registry | undefined>;
  let reg = store[KEY];
  if (!reg) {
    reg = {
      child: null,
      pid: null,
      startedAt: null,
      candidate: null,
      lastRun: null,
      ring: [],
      lineSubs: new Set(),
      endSubs: new Set(),
      tailTimer: null,
      tailOffset: 0,
      tailPartial: "",
      tailBusy: false,
      seeded: false,
      lastActivityAt: 0,
      externalPid: null,
      externalAt: 0,
    };
    store[KEY] = reg;
  }
  return reg;
}

/* ------------------------------------------------------------- log plumbing */

function pushLines(lines: string[]): void {
  const reg = registry();
  if (lines.length === 0) return;
  reg.ring.push(...lines);
  if (reg.ring.length > RING_LIMIT) reg.ring.splice(0, reg.ring.length - RING_LIMIT);
  reg.lastActivityAt = Date.now();
  for (const listener of reg.lineSubs) {
    try {
      listener(lines);
    } catch {
      /* a dead SSE client must never break the tailer */
    }
  }
}

async function readTailOfFile(limit: number): Promise<{ lines: string[]; size: number }> {
  try {
    const stat = await fsp.stat(PATHS.log);
    const span = Math.min(stat.size, 512 * 1024);
    const handle = await fsp.open(PATHS.log, "r");
    try {
      const buffer = Buffer.allocUnsafe(span);
      const { bytesRead } = await handle.read(buffer, 0, span, stat.size - span);
      const lines = buffer.subarray(0, bytesRead).toString("utf8").split(/\r?\n/);
      if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();
      return { lines: lines.slice(-limit), size: stat.size };
    } finally {
      await handle.close();
    }
  } catch {
    return { lines: [], size: 0 };
  }
}

async function pump(): Promise<void> {
  const reg = registry();
  if (reg.tailBusy) return;
  reg.tailBusy = true;
  try {
    const stat = await fsp.stat(PATHS.log).catch(() => null);
    if (!stat) return;
    if (stat.size < reg.tailOffset) {
      reg.tailOffset = 0;
      reg.tailPartial = "";
    }
    if (stat.size === reg.tailOffset) return;

    const length = Math.min(stat.size - reg.tailOffset, MAX_CHUNK);
    const handle = await fsp.open(PATHS.log, "r");
    let chunk: string;
    try {
      const buffer = Buffer.allocUnsafe(length);
      const { bytesRead } = await handle.read(buffer, 0, length, reg.tailOffset);
      reg.tailOffset += bytesRead;
      chunk = buffer.subarray(0, bytesRead).toString("utf8");
    } finally {
      await handle.close();
    }

    const parts = (reg.tailPartial + chunk).split(/\r?\n/);
    reg.tailPartial = parts.pop() ?? "";
    pushLines(parts);
  } catch {
    /* transient fs errors are not worth tearing the stream down for */
  } finally {
    reg.tailBusy = false;
  }
}

/** Seed the ring with existing history and start following the file. */
export async function ensureTail(): Promise<void> {
  const reg = registry();
  if (!reg.seeded) {
    const { lines, size } = await readTailOfFile(RING_LIMIT);
    reg.ring = lines;
    reg.tailOffset = size;
    reg.tailPartial = "";
    reg.seeded = true;
  }
  if (!reg.tailTimer) {
    const timer = setInterval(() => void pump(), POLL_MS);
    timer.unref?.();
    reg.tailTimer = timer;
  }
}

function stopTailIfIdle(): void {
  const reg = registry();
  if (reg.tailTimer && reg.lineSubs.size === 0 && !reg.child) {
    clearInterval(reg.tailTimer);
    reg.tailTimer = null;
  }
}

export function subscribe(onLines: LineListener, onEnd: EndListener): () => void {
  const reg = registry();
  reg.lineSubs.add(onLines);
  reg.endSubs.add(onEnd);
  void ensureTail();
  return () => {
    reg.lineSubs.delete(onLines);
    reg.endSubs.delete(onEnd);
    stopTailIfIdle();
  };
}

/** Last `limit` log lines; reads from disk when nothing is cached yet. */
export async function tailLines(limit: number): Promise<string[]> {
  const reg = registry();
  if (reg.seeded) {
    await pump();
    return reg.ring.slice(-limit);
  }
  const { lines } = await readTailOfFile(limit);
  return lines;
}

export function ringSnapshot(limit = RING_LIMIT): string[] {
  return registry().ring.slice(-limit);
}

export function lastActivityAt(): number {
  return registry().lastActivityAt;
}

async function appendLog(line: string): Promise<void> {
  try {
    await fsp.mkdir(PATHS.logDir, { recursive: true });
    await fsp.appendFile(PATHS.log, `${line}${os.EOL}`, "utf8");
  } catch {
    /* logging must never break a run */
  }
}

/* ----------------------------------------------------------------- run state */

function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/**
 * The bot can also be launched straight from a terminal. Because it drives one
 * shared persistent Chrome profile, the dashboard has to see those runs too or
 * it would start a second, conflicting Chrome.
 */
async function externalRunPid(): Promise<number | null> {
  const reg = registry();
  const now = Date.now();
  if (now - reg.externalAt < EXTERNAL_TTL_MS) return reg.externalPid;
  reg.externalAt = now;

  try {
    const { stdout } = await execFileAsync("pgrep", ["-f", "jobbot\\.cli (apply|run)"], {
      timeout: 2_000,
    });
    const pids = stdout
      .split(/\s+/)
      .map((value) => Number.parseInt(value, 10))
      .filter((value) => Number.isInteger(value) && value > 0 && value !== process.pid);
    reg.externalPid = pids.length > 0 ? (pids[0] as number) : null;
  } catch {
    // pgrep exits 1 when nothing matches, and may be absent entirely.
    reg.externalPid = null;
  }
  return reg.externalPid;
}

function finishRun(exitCode: number | null, error: string | null): void {
  const reg = registry();
  if (!reg.child && !reg.pid) return;
  reg.lastRun = {
    candidate: reg.candidate,
    startedAt: reg.startedAt,
    endedAt: new Date().toISOString(),
    exitCode,
    error,
  };
  reg.child = null;
  reg.pid = null;
  reg.startedAt = null;
  reg.candidate = null;
  const snapshot: RunStatus = {
    running: false,
    pid: null,
    startedAt: null,
    candidate: null,
    source: null,
    lastRun: reg.lastRun,
  };
  for (const listener of reg.endSubs) {
    try {
      listener(snapshot);
    } catch {
      /* ignore */
    }
  }
  stopTailIfIdle();
}

export async function getStatus(): Promise<RunStatus> {
  const reg = registry();

  // Reap a child whose exit event we somehow missed.
  if (reg.pid !== null && !pidAlive(reg.pid)) finishRun(null, null);

  if (reg.pid !== null) {
    return {
      running: true,
      pid: reg.pid,
      startedAt: reg.startedAt,
      candidate: reg.candidate,
      source: "dashboard",
      lastRun: reg.lastRun,
    };
  }

  const external = await externalRunPid();
  if (external !== null) {
    return {
      running: true,
      pid: external,
      startedAt: null,
      candidate: null,
      source: "external",
      lastRun: reg.lastRun,
    };
  }

  return {
    running: false,
    pid: null,
    startedAt: null,
    candidate: null,
    source: null,
    lastRun: reg.lastRun,
  };
}

/* --------------------------------------------------------------------- spawn */

async function resolveUv(): Promise<string> {
  const candidates = [
    process.env.JOBBOT_UV?.trim(),
    path.join(os.homedir(), ".local", "bin", "uv"),
    "/opt/homebrew/bin/uv",
    "/usr/local/bin/uv",
  ].filter((value): value is string => !!value);

  for (const candidate of candidates) {
    try {
      await fsp.access(candidate);
      return candidate;
    } catch {
      /* try the next one */
    }
  }
  return "uv"; // fall back to PATH lookup
}

export interface StartOutcome {
  ok: boolean;
  status: RunStatus;
  conflict?: RunStatus;
  error?: string;
  command?: string;
}

export async function startRun(candidate: RunTarget, limit: number): Promise<StartOutcome> {
  const current = await getStatus();
  if (current.running) return { ok: false, status: current, conflict: current };

  const reg = registry();
  const uv = await resolveUv();
  const args = [
    "run",
    "python",
    "-m",
    "jobbot.cli",
    "apply",
    "--candidate",
    candidate,
    "--continuous",
    "--workers",
    String(Math.max(1, Math.min(4, Number(process.env.JOBBOT_WORKERS) || 2))),
    "--india-remote-only",
    "--min-score",
    "50",
    "--min-score-floor",
    "40",
    "--skip-ats",
    "greenhouse,lever",
  ];
  const command = `${uv} ${args.join(" ")}`;

  await fsp.mkdir(PATHS.logDir, { recursive: true });
  // A previous Stop leaves STOP marker files behind; clear them so this run lives.
  await clearStopMarkers();
  void limit;
  await ensureTail();
  await appendLog(
    `[dashboard] ${new Date().toISOString()} starting ${candidateLabel(candidate)} (limit ${limit}): ${command}`,
  );

  let handle: FileHandle;
  try {
    handle = await fsp.open(PATHS.log, "a");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return { ok: false, status: current, error: `cannot open log file: ${message}` };
  }

  let child: ChildProcess;
  try {
    child = spawn(uv, args, {
      cwd: PATHS.root,
      // Own process group + unref: restarting the dashboard will not kill a
      // live Chrome automation run mid-application.
      detached: true,
      stdio: ["ignore", handle.fd, handle.fd],
      env: { ...process.env, PYTHONUNBUFFERED: "1" },
    });
  } catch (err) {
    await handle.close();
    const message = err instanceof Error ? err.message : String(err);
    await appendLog(`[dashboard] failed to start: ${message}`);
    return { ok: false, status: await getStatus(), error: message };
  }

  reg.child = child;
  reg.pid = child.pid ?? null;
  reg.startedAt = new Date().toISOString();
  reg.candidate = candidate;
  reg.lastRun = null;

  child.unref();
  child.on("error", (err) => {
    void appendLog(`[dashboard] process error: ${err.message}`);
    finishRun(null, err.message);
  });
  child.on("exit", (code, signal) => {
    void appendLog(
      `[dashboard] run finished (${code === null ? `signal ${signal}` : `exit ${code}`})`,
    );
    finishRun(code, signal ? `terminated by ${signal}` : null);
  });

  // The child inherited its own descriptor; ours is no longer needed.
  await handle.close().catch(() => undefined);

  return { ok: true, status: await getStatus(), command };
}

/* ---------------------------------------------------------------------- stop */

function stopMarkerDir(): string {
  return path.join(PATHS.root, "data");
}

async function clearStopMarkers(): Promise<void> {
  const dir = stopMarkerDir();
  for (const name of ["STOP", "STOP-harshank", "STOP-divya"]) {
    await fsp.rm(path.join(dir, name), { force: true }).catch(() => undefined);
  }
}

async function allBotPids(): Promise<number[]> {
  try {
    const { stdout } = await execFileAsync("pgrep", ["-f", "jobbot\\.cli (apply|run)"], {
      timeout: 2_000,
    });
    return stdout
      .split(/\s+/)
      .map((value) => Number.parseInt(value, 10))
      .filter((value) => Number.isInteger(value) && value > 0 && value !== process.pid);
  } catch {
    return [];
  }
}

/**
 * Stops every running bot (dashboard- or terminal-started). Writes data/STOP so
 * the workers exit cleanly, then terminates the processes so Chrome closes.
 */
export async function stopRun(): Promise<{ stopped: number[]; status: RunStatus }> {
  await fsp.mkdir(stopMarkerDir(), { recursive: true }).catch(() => undefined);
  await fsp.writeFile(path.join(stopMarkerDir(), "STOP"), new Date().toISOString(), "utf8");
  const reg = registry();
  const pids = new Set(await allBotPids());
  if (reg.pid !== null) pids.add(reg.pid);
  for (const pid of pids) {
    try {
      process.kill(pid, "SIGTERM");
    } catch {
      /* already gone */
    }
  }
  reg.externalAt = 0;
  await appendLog(`[dashboard] ${new Date().toISOString()} stop requested (pids ${[...pids].join(", ") || "none"})`);
  await new Promise((resolve) => setTimeout(resolve, 1500));
  if (reg.pid !== null && !pidAlive(reg.pid)) finishRun(null, "stopped by user");
  return { stopped: [...pids], status: await getStatus() };
}

export const LOG_FILE = PATHS.log;
