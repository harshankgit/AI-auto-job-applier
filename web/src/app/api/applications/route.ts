import { withMac } from "@/lib/mac-proxy";
import { NextResponse } from "next/server";

import { computeStats, loadApplications } from "@/lib/applications";
import type { ApplicationsPayload } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Always 200: an unreadable source degrades to an empty set plus `error`. */
async function handleGET() {
  try {
    const payload = await loadApplications();
    return NextResponse.json(payload, {
      headers: { "cache-control": "no-store" },
    });
  } catch (err) {
    const fallback: ApplicationsPayload = {
      rows: [],
      stats: computeStats([]),
      source: "none",
      error: err instanceof Error ? err.message : String(err),
    };
    return NextResponse.json(fallback, { headers: { "cache-control": "no-store" } });
  }
}

export const GET = withMac(handleGET);
