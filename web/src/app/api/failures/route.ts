import fsp from "node:fs/promises";
import { NextResponse } from "next/server";

import { PATHS } from "@/lib/paths";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Success / failure breakdown written by `python -m jobbot.learn` (and by every live run). */
export async function GET() {
  try {
    const [raw, stat] = await Promise.all([fsp.readFile(PATHS.failures, "utf8"), fsp.stat(PATHS.failures)]);
    return NextResponse.json(
      { ...JSON.parse(raw), updatedAt: stat.mtime.toISOString() },
      { headers: { "cache-control": "no-store" } },
    );
  } catch {
    return NextResponse.json(
      { generated_at: null, candidates: {}, error: "no failure report yet - it is written while the bot runs" },
      { headers: { "cache-control": "no-store" } },
    );
  }
}
