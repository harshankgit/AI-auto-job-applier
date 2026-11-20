import type { CandidateMeta } from "./types";

export type CandidateId = "harshank" | "divya";
export type RunTarget = CandidateId | "both";

export const CANDIDATE_IDS: readonly CandidateId[] = ["harshank", "divya"];
export const candidateIds = CANDIDATE_IDS;

export function isCandidateId(value: string): value is CandidateId {
  return (CANDIDATE_IDS as readonly string[]).includes(value);
}

export function isRunTarget(value: unknown): value is RunTarget {
  return typeof value === "string" && (value === "both" || isCandidateId(value));
}

/**
 * Candidate metadata the dashboard presents. Contact details are only listed
 * here when they were supplied directly; anything else is read from the real
 * attempt rows (email_used / phone_used) or rendered as an em dash.
 */
export const CANDIDATES: readonly CandidateMeta[] = [
  {
    id: "harshank",
    name: "Harshank Kanungo",
    title: "Full Stack Developer",
    yearsExperience: 3,
    expectedCtc: "14-15 LPA",
    email: "harshankkanungo259@gmail.com",
    phone: "+91 8109774968",
    location: "Gurugram, Haryana, India",
    defaultResume: "/Users/apple/Desktop/AI/harshank/Harshank_Kanungo_Full_Stack_Developer_ATS.pdf",
  },
  {
    id: "divya",
    name: "Divya Kanungo",
    title: "MERN Stack Developer",
    yearsExperience: 1,
    expectedCtc: "4-5 LPA",
    email: null,
    phone: null,
    location: "Gurugram, Haryana, India",
    defaultResume: "/Users/apple/Desktop/AI/resume/Divya_Kanungo_MERN_Stack_Developer.pdf",
  },
];

export function candidateMeta(id: string): CandidateMeta | undefined {
  return CANDIDATES.find((candidate) => candidate.id === id);
}

export function candidateLabel(id: string | null): string {
  if (!id) return "—";
  if (id === "both") return "Both candidates";
  return candidateMeta(id)?.name ?? id;
}

/** Per-candidate accent so the two cards read as parallel but distinct. */
export const ACCENTS = {
  harshank: {
    text: "text-indigo-300",
    dot: "bg-indigo-400",
    bar: "bg-indigo-500/60",
    ring: "ring-indigo-500/20",
    button: "bg-indigo-500 hover:bg-indigo-400 text-white",
    soft: "bg-indigo-500/10 text-indigo-200 ring-indigo-500/25",
  },
  divya: {
    text: "text-teal-300",
    dot: "bg-teal-400",
    bar: "bg-teal-500/60",
    ring: "ring-teal-500/20",
    button: "bg-teal-500 hover:bg-teal-400 text-zinc-950",
    soft: "bg-teal-500/10 text-teal-200 ring-teal-500/25",
  },
} as const satisfies Record<CandidateId, Record<string, string>>;

export function accentFor(id: string) {
  return id === "divya" ? ACCENTS.divya : ACCENTS.harshank;
}
