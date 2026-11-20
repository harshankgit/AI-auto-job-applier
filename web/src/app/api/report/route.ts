import fsp from "node:fs/promises";

import { NextResponse } from "next/server";

import { PATHS } from "@/lib/paths";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET() {
  let markdown = "";
  let updatedAt: string | null = null;
  try {
    markdown = await fsp.readFile(PATHS.markdown, "utf8");
    const stat = await fsp.stat(PATHS.markdown);
    updatedAt = stat.mtime.toISOString();
  } catch {
    markdown = "";
  }
  return NextResponse.json(
    { markdown, updatedAt, path: PATHS.markdown },
    { headers: { "cache-control": "no-store" } },
  );
}
