export type DataSource = "sqlite" | "csv" | "none";

export const STATUS_VALUES = [
  "SUBMITTED",
  "SKIPPED",
  "CLOSED",
  "ALREADY_APPLIED",
  "LOGIN_REQUIRED",
  "ERROR",
  "FAILED_VALIDATION",
  "FAILED",
] as const;

export type KnownStatus = (typeof STATUS_VALUES)[number];

/**
 * One attempt row. The sqlite `attempts` table and the CSV report expose
 * overlapping-but-different column sets, so fields absent from whichever
 * source was used are `null` — never invented.
 */
export interface Application {
  id: number | null;
  jobKey: string | null;
  candidateId: string;
  candidateName: string | null;
  emailUsed: string | null;
  phoneUsed: string | null;
  candidateLocation: string | null;
  company: string | null;
  title: string | null;
  jobLocation: string | null;
  source: string | null;
  companyType: string | null;
  status: string;
  detail: string | null;
  score: number | null;
  resumeFile: string | null;
  resumeAttached: boolean | null;
  fieldsFilled: number | null;
  fieldsSkipped: string | null;
  linkedin: string | null;
  github: string | null;
  portfolio: string | null;
  matchReasons: string | null;
  applyUrl: string | null;
  screenshot: string | null;
  confirmationText: string | null;
  /** ISO-8601 UTC string, or null when the source had no parseable timestamp. */
  appliedAt: string | null;
}

export interface CandidateStats {
  total: number;
  submitted: number;
  failed: number;
  companies: number;
  avgScore: number | null;
  lastRunAt: string | null;
}

export interface Stats {
  total: number;
  submitted: number;
  failed: number;
  skipped: number;
  companies: number;
  avgScore: number | null;
  lastRunAt: string | null;
  byStatus: Record<string, number>;
  byCandidate: Record<string, CandidateStats>;
}

export interface ApplicationsPayload {
  rows: Application[];
  stats: Stats;
  source: DataSource;
  /** Populated when a source was present but unreadable; UI surfaces it. */
  error: string | null;
}

export type RunSource = "dashboard" | "external" | "cloud";

export interface LastRunInfo {
  candidate: string | null;
  startedAt: string | null;
  endedAt: string;
  exitCode: number | null;
  error: string | null;
}

export interface RunStatus {
  running: boolean;
  pid: number | null;
  startedAt: string | null;
  candidate: string | null;
  /** Extra context for the UI: whether the dashboard or a terminal owns it. */
  source: RunSource | null;
  /** Set once a dashboard-launched run has finished, so the UI can report it. */
  lastRun: LastRunInfo | null;
}

export interface CandidateMeta {
  id: string;
  name: string;
  title: string;
  yearsExperience: number;
  expectedCtc: string;
  email: string | null;
  phone: string | null;
  location: string;
  defaultResume: string;
}
