import fsp from "node:fs/promises";
import path from "node:path";

import { PATHS } from "./paths";

/**
 * `config/profiles.yaml` is hand-maintained and carries comments plus a
 * deliberate key order, so it is never round-tripped through a YAML
 * serializer. We locate the one `resume:` line that belongs to the requested
 * candidate block and rewrite just that line's value, byte-for-byte preserving
 * everything else (indentation, comments, trailing whitespace, line endings).
 */
const ID_LINE = /^(\s*)-\s+id:\s*(.+?)\s*$/;
const RESUME_LINE = /^(\s*)resume:(\s*)(.*)$/;

function stripQuotes(raw: string): string {
  const value = raw.trim();
  if (value.length >= 2) {
    const first = value[0];
    const last = value[value.length - 1];
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      return value.slice(1, -1);
    }
  }
  return value;
}

/** Split `"/some/path"   # note` into its scalar and any trailing comment. */
function splitScalar(raw: string): { value: string; trailer: string } {
  const quote = raw[0];
  if (quote === '"' || quote === "'") {
    for (let i = 1; i < raw.length; i += 1) {
      if (raw[i] === "\\" && quote === '"') {
        i += 1;
        continue;
      }
      if (raw[i] === quote) {
        if (quote === "'" && raw[i + 1] === "'") {
          i += 1;
          continue;
        }
        return { value: stripQuotes(raw.slice(0, i + 1)), trailer: raw.slice(i + 1) };
      }
    }
    return { value: stripQuotes(raw), trailer: "" };
  }
  const comment = raw.search(/\s#/);
  if (comment === -1) return { value: raw.trim(), trailer: "" };
  return { value: raw.slice(0, comment).trim(), trailer: raw.slice(comment) };
}

function yamlDoubleQuote(value: string): string {
  return `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`;
}

export interface ProfileResume {
  candidateId: string;
  resume: string | null;
}

async function readProfilesText(): Promise<string | null> {
  try {
    return await fsp.readFile(PATHS.profiles, "utf8");
  } catch {
    return null;
  }
}

/** Current resume path per candidate, straight out of profiles.yaml. */
export async function readProfileResumes(): Promise<Record<string, string | null>> {
  const text = await readProfilesText();
  if (text === null) return {};

  const out: Record<string, string | null> = {};
  let current: string | null = null;

  for (const line of text.split(/\r?\n/)) {
    const idMatch = ID_LINE.exec(line);
    if (idMatch) {
      current = stripQuotes(idMatch[2] ?? "");
      if (current && !(current in out)) out[current] = null;
      continue;
    }
    const resumeMatch = RESUME_LINE.exec(line);
    if (resumeMatch && current) {
      const { value } = splitScalar(resumeMatch[3] ?? "");
      out[current] = value === "" ? null : value;
    }
  }
  return out;
}

export class ProfileEditError extends Error {}

/**
 * Point a candidate's `resume:` key at `absolutePath`.
 * Writes through a sibling temp file + rename so a concurrently-running bot
 * never observes a half-written config.
 */
export async function setCandidateResume(candidateId: string, absolutePath: string): Promise<string> {
  const text = await readProfilesText();
  if (text === null) {
    throw new ProfileEditError(`cannot read ${PATHS.profiles}`);
  }

  const newline = text.includes("\r\n") ? "\r\n" : "\n";
  const lines = text.split(/\r?\n/);

  let inBlock = false;
  let target = -1;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i] as string;
    const idMatch = ID_LINE.exec(line);
    if (idMatch) {
      inBlock = stripQuotes(idMatch[2] ?? "") === candidateId;
      continue;
    }
    if (!inBlock) continue;
    if (RESUME_LINE.test(line)) {
      target = i;
      break;
    }
  }

  if (target === -1) {
    throw new ProfileEditError(
      `no "resume:" key found for candidate "${candidateId}" in ${path.basename(PATHS.profiles)}`,
    );
  }

  const match = RESUME_LINE.exec(lines[target] as string);
  const indent = match?.[1] ?? "    ";
  const gap = match?.[2] === "" ? " " : (match?.[2] ?? " ");
  const { trailer } = splitScalar(match?.[3] ?? "");
  lines[target] = `${indent}resume:${gap}${yamlDoubleQuote(absolutePath)}${trailer}`;

  const tmp = path.join(path.dirname(PATHS.profiles), `.profiles.yaml.${process.pid}.tmp`);
  await fsp.writeFile(tmp, lines.join(newline), "utf8");
  await fsp.rename(tmp, PATHS.profiles);
  return absolutePath;
}
