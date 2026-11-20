import { withMac } from "@/lib/mac-proxy";
import { NextResponse } from "next/server";

import { candidateLabel, isRunTarget } from "@/lib/candidates";
import { rootIsPresent } from "@/lib/paths";
import { getStatus, startRun } from "@/lib/runner";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const DEFAULT_LIMIT = 40;
const MAX_LIMIT = 100;

function parseLimit(value: unknown): number {
  if (value === undefined || value === null || value === "") return DEFAULT_LIMIT;
  const parsed = Math.trunc(Number(value));
  if (!Number.isFinite(parsed) || parsed < 1) return DEFAULT_LIMIT;
  return Math.min(parsed, MAX_LIMIT);
}

/**
 * Launches the real bot: `uv run python -m jobbot.cli apply ...`
 * Never with --show (Chrome stays parked off-screen) and never --dry-run.
 * One run at a time, because the bot drives a single shared Chrome profile.
 */
async function handlePOST(request: Request) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    body = {};
  }

  const payload = (body ?? {}) as { candidate?: unknown; limit?: unknown };
  if (!isRunTarget(payload.candidate)) {
    return NextResponse.json(
      { error: 'candidate must be one of "harshank", "divya" or "both"' },
      { status: 400 },
    );
  }

  if (!rootIsPresent()) {
    return NextResponse.json(
      {
        error:
          "the jobbot project root is not reachable from this server, so the Python bot cannot be launched here",
      },
      { status: 503 },
    );
  }

  const limit = parseLimit(payload.limit);
  const outcome = await startRun(payload.candidate, limit);

  if (!outcome.ok && outcome.conflict) {
    const owner = outcome.conflict.source === "external" ? "a terminal" : "the dashboard";
    return NextResponse.json(
      {
        error: `a run started from ${owner} is already active — only one Chrome automation can use data/chrome-profile at a time`,
        status: outcome.conflict,
      },
      { status: 409 },
    );
  }

  if (!outcome.ok) {
    return NextResponse.json(
      { error: outcome.error ?? "could not start the run", status: await getStatus() },
      { status: 500 },
    );
  }

  return NextResponse.json({
    started: true,
    candidate: payload.candidate,
    candidateLabel: candidateLabel(payload.candidate),
    limit,
    command: outcome.command ?? null,
    status: outcome.status,
  });
}

export const POST = withMac(handlePOST);
