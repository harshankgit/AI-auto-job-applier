import { NextResponse } from "next/server";

import { getStatus } from "@/lib/runner";
import type { RunStatus } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const status = await getStatus();
    return NextResponse.json(status, { headers: { "cache-control": "no-store" } });
  } catch {
    const idle: RunStatus = {
      running: false,
      pid: null,
      startedAt: null,
      candidate: null,
      source: null,
      lastRun: null,
    };
    return NextResponse.json(idle, { headers: { "cache-control": "no-store" } });
  }
}
