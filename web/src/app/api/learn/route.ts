import { withMac } from "@/lib/mac-proxy";
import fsp from "node:fs/promises";
import { NextResponse } from "next/server";

import { isRunTarget } from "@/lib/candidates";
import { PATHS, rootIsPresent } from "@/lib/paths";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Same normalisation as jobbot/learn.py norm(). */
function norm(label: string): string {
  return label
    .toLowerCase()
    .replace(/\u2731/g, " ")
    .replace(/\*/g, " ")
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

type Learned = Record<string, Record<string, { question: string; answer: string; at: string }>>;

async function readJson<T>(file: string, fallback: T): Promise<T> {
  try {
    return JSON.parse(await fsp.readFile(file, "utf8")) as T;
  } catch {
    return fallback;
  }
}

/** Answers are stored only on the bot machine (data/ is git-ignored), never in the repo. */
async function handleGET() {
  if (!rootIsPresent()) return NextResponse.json({ learned: {}, readOnly: true });
  const data = await readJson<Learned>(PATHS.learned, {});
  // Only expose which questions are answered, plus the answers themselves for the owner's machine.
  return NextResponse.json({ learned: data, readOnly: false }, { headers: { "cache-control": "no-store" } });
}

async function handlePOST(req: Request) {
  if (!rootIsPresent()) {
    return NextResponse.json(
      { error: "answers can only be saved on the machine that runs the bot (open the dashboard on that Mac)" },
      { status: 503 },
    );
  }
  let body: { candidate?: unknown; question?: unknown; answer?: unknown; answers?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid JSON body" }, { status: 400 });
  }
  const candidate = body.candidate;
  if (!isRunTarget(candidate)) {
    return NextResponse.json({ error: "candidate must be harshank, divya or both" }, { status: 400 });
  }
  const items: { question: string; answer: string }[] = [];
  if (Array.isArray(body.answers)) {
    for (const a of body.answers as { question?: unknown; answer?: unknown }[]) {
      if (typeof a?.question === "string" && typeof a?.answer === "string") items.push({ question: a.question, answer: a.answer });
    }
  } else if (typeof body.question === "string" && typeof body.answer === "string") {
    items.push({ question: body.question, answer: body.answer });
  }
  const clean = items
    .map((i) => ({ question: i.question.trim().slice(0, 500), answer: i.answer.trim().slice(0, 2000) }))
    .filter((i) => i.question && norm(i.question));
  if (!clean.length) return NextResponse.json({ error: "nothing to save" }, { status: 400 });

  const data = await readJson<Learned>(PATHS.learned, {});
  const at = new Date().toISOString().replace("T", " ").slice(0, 19);
  for (const { question, answer } of clean) {
    const scope = (data[candidate] ??= {});
    if (answer) scope[norm(question)] = { question, answer, at };
    else delete scope[norm(question)];
  }
  await fsp.mkdir(PATHS.dataDir, { recursive: true });
  const tmp = `${PATHS.learned}.tmp`;
  await fsp.writeFile(tmp, JSON.stringify(data, null, 2));
  await fsp.rename(tmp, PATHS.learned);
  return NextResponse.json({ saved: clean.length, candidate });
}

export const GET = withMac(handleGET);
export const POST = withMac(handlePOST);
