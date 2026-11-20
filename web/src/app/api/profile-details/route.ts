import { withMac } from "@/lib/mac-proxy";
import fsp from "node:fs/promises";
import { NextResponse } from "next/server";

import { isCandidateId } from "@/lib/candidates";
import { PATHS, rootIsPresent } from "@/lib/paths";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * Private candidate details (current CTC, GitHub, work authorisation...).
 * Saved to data/private_profile.json on the bot machine only - data/ is
 * git-ignored, so none of this is committed or shown on the public site.
 */
const FIELDS = ["github", "linkedin", "portfolio", "current_ctc", "expected_ctc", "notice_period", "work_authorized_countries"] as const;
type Field = (typeof FIELDS)[number];
type Private = Record<string, Record<string, unknown>>;

async function load(): Promise<Private> {
  try {
    return JSON.parse(await fsp.readFile(PATHS.privateProfile, "utf8")) as Private;
  } catch {
    return {};
  }
}

async function handleGET(req: Request) {
  if (!rootIsPresent()) return NextResponse.json({ details: {}, readOnly: true });
  const cid = new URL(req.url).searchParams.get("candidate") ?? "";
  if (!isCandidateId(cid)) return NextResponse.json({ error: "unknown candidate" }, { status: 400 });
  const row = (await load())[cid] ?? {};
  const details: Partial<Record<Field, string>> = {};
  for (const f of FIELDS) {
    const v = row[f];
    if (Array.isArray(v)) details[f] = v.join(", ");
    else if (v != null) details[f] = String(v);
  }
  return NextResponse.json({ details, readOnly: false }, { headers: { "cache-control": "no-store" } });
}

async function handlePOST(req: Request) {
  if (!rootIsPresent()) {
    return NextResponse.json({ error: "details can only be saved on the machine that runs the bot" }, { status: 503 });
  }
  let body: { candidate?: unknown; details?: Record<string, unknown> };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const cid = typeof body.candidate === "string" ? body.candidate : "";
  if (!isCandidateId(cid)) return NextResponse.json({ error: "unknown candidate" }, { status: 400 });
  const all = await load();
  const row = { ...(all[cid] ?? {}) };
  for (const f of FIELDS) {
    const raw = body.details?.[f];
    if (typeof raw !== "string") continue;
    const v = raw.trim().slice(0, 300);
    if (!v) delete row[f];
    else if (f === "work_authorized_countries") row[f] = v.split(",").map((s) => s.trim()).filter(Boolean);
    else row[f] = v;
  }
  all[cid] = row;
  await fsp.mkdir(PATHS.dataDir, { recursive: true });
  const tmp = `${PATHS.privateProfile}.tmp`;
  await fsp.writeFile(tmp, JSON.stringify(all, null, 2));
  await fsp.rename(tmp, PATHS.privateProfile);
  return NextResponse.json({ saved: true, note: "the running bot picks this up on its next restart" });
}

export const GET = withMac(handleGET);
export const POST = withMac(handlePOST);
