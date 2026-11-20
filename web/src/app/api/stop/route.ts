import { NextResponse } from "next/server";

import { rootIsPresent } from "@/lib/paths";
import { stopRun } from "@/lib/runner";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST() {
  if (!rootIsPresent()) {
    return NextResponse.json(
      { error: "the jobbot project root is not reachable from this server, so there is no run to stop here" },
      { status: 503 },
    );
  }
  try {
    const out = await stopRun();
    return NextResponse.json({ stopped: true, pids: out.stopped, status: out.status });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "failed to stop the run" },
      { status: 500 },
    );
  }
}
