import fsp from "node:fs/promises";
import path from "node:path";

import { NextResponse } from "next/server";

import { isCandidateId } from "@/lib/candidates";
import { PATHS, rootIsPresent } from "@/lib/paths";
import { ProfileEditError, setCandidateResume } from "@/lib/profiles";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ALLOWED = [".pdf", ".doc", ".docx"] as const;
const MAX_BYTES = 10 * 1024 * 1024;

function sanitize(name: string): string {
  const base = path.basename(name).replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^[._]+/, "");
  const trimmed = base.slice(0, 120);
  return trimmed.length > 0 ? trimmed : "resume";
}

function stamp(): string {
  return new Date().toISOString().replace(/[:.]/g, "-").replace("Z", "Z");
}

/**
 * Stores an uploaded resume under data/resumes/<candidate>/ and repoints that
 * candidate's `resume:` value in config/profiles.yaml at the new file. The YAML
 * is edited line-wise, so comments, key order and formatting all survive.
 */
export async function POST(request: Request) {
  if (!rootIsPresent()) {
    return NextResponse.json(
      { error: "the jobbot project root is not reachable from this server" },
      { status: 503 },
    );
  }

  let form: FormData;
  try {
    form = await request.formData();
  } catch {
    return NextResponse.json({ error: "expected a multipart form upload" }, { status: 400 });
  }

  const candidate = String(form.get("candidate") ?? "");
  if (!isCandidateId(candidate)) {
    return NextResponse.json(
      { error: 'candidate must be "harshank" or "divya"' },
      { status: 400 },
    );
  }

  const file = form.get("file");
  if (!(file instanceof File)) {
    return NextResponse.json({ error: "no file was attached under the \"file\" field" }, { status: 400 });
  }

  const extension = path.extname(file.name).toLowerCase();
  if (!(ALLOWED as readonly string[]).includes(extension)) {
    return NextResponse.json(
      { error: `unsupported file type "${extension || "unknown"}" — use PDF, DOC or DOCX` },
      { status: 415 },
    );
  }

  if (file.size <= 0) {
    return NextResponse.json({ error: "the uploaded file is empty" }, { status: 400 });
  }
  if (file.size > MAX_BYTES) {
    const mb = (file.size / (1024 * 1024)).toFixed(1);
    return NextResponse.json(
      { error: `file is ${mb} MB — the limit is 10 MB` },
      { status: 413 },
    );
  }

  const targetDir = path.join(PATHS.resumeUploads, candidate);
  const filename = `${stamp()}-${sanitize(file.name)}`;
  const destination = path.join(targetDir, filename);

  try {
    await fsp.mkdir(targetDir, { recursive: true });
    await fsp.writeFile(destination, Buffer.from(await file.arrayBuffer()));
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json({ error: `could not save the file: ${message}` }, { status: 500 });
  }

  try {
    await setCandidateResume(candidate, destination);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    const status = err instanceof ProfileEditError ? 422 : 500;
    return NextResponse.json(
      {
        error: `file saved to ${destination} but profiles.yaml was not updated: ${message}`,
        path: destination,
      },
      { status },
    );
  }

  return NextResponse.json({
    ok: true,
    candidate,
    path: destination,
    filename,
    originalName: file.name,
    bytes: file.size,
  });
}
