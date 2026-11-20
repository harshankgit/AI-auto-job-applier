import fs from "node:fs/promises";

import { parseCsv } from "./csv";
import { PATHS } from "./paths";
import type { Application, ApplicationsPayload, CandidateStats, DataSource, Stats } from "./types";

const FAILED_STATUSES = new Set(["ERROR", "FAILED", "FAILED_VALIDATION"]);

/* ------------------------------------------------------------------ helpers */

function str(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  const out = String(value).trim();
  return out === "" ? null : out;
}

function num(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const out = Number(value);
  return Number.isFinite(out) ? out : null;
}

function int(value: unknown): number | null {
  const out = num(value);
  return out === null ? null : Math.trunc(out);
}

function bool(value: unknown): boolean | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "boolean") return value;
  const out = String(value).trim().toLowerCase();
  if (["1", "yes", "true", "y"].includes(out)) return true;
  if (["0", "no", "false", "n"].includes(out)) return false;
  return null;
}

/** The bot writes naive UTC timestamps ("2026-11-19 11:51:48"). */
export function toIsoUtc(value: unknown): string | null {
  const raw = str(value);
  if (!raw) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw);
  const normalized = hasZone ? raw.replace(" ", "T") : `${raw.replace(" ", "T")}Z`;
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) {
    const fallback = new Date(raw);
    return Number.isNaN(fallback.getTime()) ? null : fallback.toISOString();
  }
  return date.toISOString();
}

/**
 * `match_reasons` is stored in sqlite as `"; ".join(<a string>)`, which
 * interleaves every character with "; ". When every segment is a single
 * character the original string is recoverable exactly by re-joining, so we
 * undo it for display. Anything else is passed through untouched.
 */
function unmangleReasons(value: string | null): string | null {
  if (!value) return value;
  const parts = value.split("; ");
  if (parts.length >= 8 && parts.every((part) => part.length <= 1)) return parts.join("");
  return value;
}

const NAME_TO_ID: Record<string, string> = {
  "harshank kanungo": "harshank",
  "divya kanungo": "divya",
};

/** CSV rows carry the display name only; derive the bot's candidate id from it. */
function candidateIdFromName(name: string | null): string {
  if (!name) return "unknown";
  const key = name.trim().toLowerCase();
  const mapped = NAME_TO_ID[key];
  if (mapped) return mapped;
  const first = key.split(/\s+/)[0];
  return first && first.length > 0 ? first : "unknown";
}

function emptyApplication(): Application {
  return {
    id: null,
    jobKey: null,
    candidateId: "unknown",
    candidateName: null,
    emailUsed: null,
    phoneUsed: null,
    candidateLocation: null,
    company: null,
    title: null,
    jobLocation: null,
    source: null,
    companyType: null,
    status: "UNKNOWN",
    detail: null,
    score: null,
    resumeFile: null,
    resumeAttached: null,
    fieldsFilled: null,
    fieldsSkipped: null,
    linkedin: null,
    github: null,
    portfolio: null,
    matchReasons: null,
    applyUrl: null,
    screenshot: null,
    confirmationText: null,
    appliedAt: null,
  };
}

/* ------------------------------------------------------------------- sqlite */

async function loadFromSqlite(): Promise<Application[] | null> {
  try {
    await fs.access(PATHS.db);
  } catch {
    return null;
  }

  try {
    const { DatabaseSync } = await import("node:sqlite");
    const db = new DatabaseSync(PATHS.db, { readOnly: true });
    try {
      const table = db
        .prepare("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'attempts'")
        .get();
      if (!table) return null;

      const raw = db
        .prepare(
          `SELECT id, job_key, candidate_id, candidate_name, email_used, phone_used, company,
                  title, job_location, source, status, detail, score, resume_file,
                  resume_attached, fields_filled, apply_url, screenshot, confirmation_text,
                  match_reasons, applied_at
             FROM attempts`,
        )
        .all() as Record<string, unknown>[];

      return raw.map((row) => ({
        ...emptyApplication(),
        id: int(row.id),
        jobKey: str(row.job_key),
        candidateId: str(row.candidate_id) ?? candidateIdFromName(str(row.candidate_name)),
        candidateName: str(row.candidate_name),
        emailUsed: str(row.email_used),
        phoneUsed: str(row.phone_used),
        company: str(row.company),
        title: str(row.title),
        jobLocation: str(row.job_location),
        source: str(row.source),
        status: str(row.status) ?? "UNKNOWN",
        detail: str(row.detail),
        score: num(row.score),
        resumeFile: str(row.resume_file),
        resumeAttached: bool(row.resume_attached),
        fieldsFilled: int(row.fields_filled),
        applyUrl: str(row.apply_url),
        screenshot: str(row.screenshot),
        confirmationText: str(row.confirmation_text),
        matchReasons: unmangleReasons(str(row.match_reasons)),
        appliedAt: toIsoUtc(row.applied_at),
      }));
    } finally {
      db.close();
    }
  } catch {
    // Missing node:sqlite, a locked/corrupt file, or a schema change: fall back.
    return null;
  }
}

/* ---------------------------------------------------------------------- csv */

const CSV_HEADERS = {
  appliedAt: "Applied At (UTC)",
  candidate: "Candidate",
  email: "Email Used",
  phone: "Phone Used",
  candidateLocation: "Candidate Location",
  company: "Company",
  title: "Job Title",
  jobLocation: "Job Location",
  source: "Source",
  companyType: "Company Type",
  score: "Match Score",
  status: "Status",
  detail: "Detail",
  resumeAttached: "Resume Attached",
  resumeFile: "Resume File",
  fieldsFilled: "Fields Filled",
  fieldsSkipped: "Fields Skipped",
  linkedin: "LinkedIn",
  github: "GitHub",
  portfolio: "Portfolio",
  matchReasons: "Why Matched",
  applyUrl: "Apply URL",
  screenshot: "Screenshot",
} as const;

async function loadFromCsv(): Promise<Application[] | null> {
  let text: string;
  try {
    text = await fs.readFile(PATHS.csv, "utf8");
  } catch {
    return null;
  }

  const table = parseCsv(text);
  const header = table[0];
  if (!header || table.length < 2) return null;

  const index = new Map<string, number>();
  header.forEach((name, i) => index.set(name.trim(), i));
  const cell = (row: string[], name: string): string | null => {
    const i = index.get(name);
    return i === undefined ? null : str(row[i]);
  };

  return table.slice(1).map((row) => {
    const candidateName = cell(row, CSV_HEADERS.candidate);
    return {
      ...emptyApplication(),
      candidateId: candidateIdFromName(candidateName),
      candidateName,
      emailUsed: cell(row, CSV_HEADERS.email),
      phoneUsed: cell(row, CSV_HEADERS.phone),
      candidateLocation: cell(row, CSV_HEADERS.candidateLocation),
      company: cell(row, CSV_HEADERS.company),
      title: cell(row, CSV_HEADERS.title),
      jobLocation: cell(row, CSV_HEADERS.jobLocation),
      source: cell(row, CSV_HEADERS.source),
      companyType: cell(row, CSV_HEADERS.companyType),
      status: cell(row, CSV_HEADERS.status) ?? "UNKNOWN",
      detail: cell(row, CSV_HEADERS.detail),
      score: num(cell(row, CSV_HEADERS.score)),
      resumeFile: cell(row, CSV_HEADERS.resumeFile),
      resumeAttached: bool(cell(row, CSV_HEADERS.resumeAttached)),
      fieldsFilled: int(cell(row, CSV_HEADERS.fieldsFilled)),
      fieldsSkipped: cell(row, CSV_HEADERS.fieldsSkipped),
      linkedin: cell(row, CSV_HEADERS.linkedin),
      github: cell(row, CSV_HEADERS.github),
      portfolio: cell(row, CSV_HEADERS.portfolio),
      matchReasons: cell(row, CSV_HEADERS.matchReasons),
      applyUrl: cell(row, CSV_HEADERS.applyUrl),
      screenshot: cell(row, CSV_HEADERS.screenshot),
      appliedAt: toIsoUtc(cell(row, CSV_HEADERS.appliedAt)),
    };
  });
}

/* -------------------------------------------------------------------- stats */

function average(values: number[]): number | null {
  if (values.length === 0) return null;
  const sum = values.reduce((acc, value) => acc + value, 0);
  return Math.round((sum / values.length) * 10) / 10;
}

function latest(rows: Application[]): string | null {
  let best: string | null = null;
  for (const row of rows) {
    if (!row.appliedAt) continue;
    if (best === null || row.appliedAt > best) best = row.appliedAt;
  }
  return best;
}

function statsFor(rows: Application[]): CandidateStats {
  return {
    total: rows.length,
    submitted: rows.filter((row) => row.status === "SUBMITTED").length,
    failed: rows.filter((row) => FAILED_STATUSES.has(row.status)).length,
    companies: new Set(rows.map((row) => row.company).filter((name): name is string => !!name)).size,
    avgScore: average(rows.map((row) => row.score).filter((score): score is number => score !== null)),
    lastRunAt: latest(rows),
  };
}

export function computeStats(rows: Application[]): Stats {
  const byStatus: Record<string, number> = {};
  for (const row of rows) byStatus[row.status] = (byStatus[row.status] ?? 0) + 1;

  const byCandidate: Record<string, CandidateStats> = {};
  for (const id of new Set(rows.map((row) => row.candidateId))) {
    byCandidate[id] = statsFor(rows.filter((row) => row.candidateId === id));
  }

  const base = statsFor(rows);
  return {
    total: base.total,
    submitted: base.submitted,
    failed: base.failed,
    skipped: byStatus.SKIPPED ?? 0,
    companies: base.companies,
    avgScore: base.avgScore,
    lastRunAt: base.lastRunAt,
    byStatus,
    byCandidate,
  };
}

/* --------------------------------------------------------------------- load */

/** Newest attempt first; rows without a timestamp sink to the bottom. */
function sortNewestFirst(rows: Application[]): Application[] {
  return [...rows].sort((a, b) => {
    if (a.appliedAt && b.appliedAt && a.appliedAt !== b.appliedAt) {
      return a.appliedAt < b.appliedAt ? 1 : -1;
    }
    if (a.appliedAt && !b.appliedAt) return -1;
    if (!a.appliedAt && b.appliedAt) return 1;
    return (b.id ?? 0) - (a.id ?? 0);
  });
}

export async function loadApplications(): Promise<ApplicationsPayload> {
  let error: string | null = null;
  let rows: Application[] | null = null;
  let source: DataSource = "none";

  try {
    rows = await loadFromSqlite();
    if (rows) source = "sqlite";
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
  }

  if (!rows) {
    try {
      rows = await loadFromCsv();
      if (rows) source = "csv";
    } catch (err) {
      error = err instanceof Error ? err.message : String(err);
    }
  }

  const resolved = sortNewestFirst(rows ?? []);
  return { rows: resolved, stats: computeStats(resolved), source, error };
}
