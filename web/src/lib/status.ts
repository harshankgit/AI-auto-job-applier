/** Status pill styling. Classes are written out in full so Tailwind keeps them. */
const STYLES: Record<string, string> = {
  SUBMITTED: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/25",
  SKIPPED: "bg-slate-500/10 text-slate-300 ring-slate-400/25",
  CLOSED: "bg-amber-500/10 text-amber-300 ring-amber-500/25",
  ALREADY_APPLIED: "bg-sky-500/10 text-sky-300 ring-sky-500/25",
  LOGIN_REQUIRED: "bg-amber-500/10 text-amber-300 ring-amber-500/25",
  ERROR: "bg-rose-500/10 text-rose-300 ring-rose-500/25",
  FAILED_VALIDATION: "bg-rose-500/10 text-rose-300 ring-rose-500/25",
  FAILED: "bg-rose-500/10 text-rose-300 ring-rose-500/25",
};

const UNKNOWN_STYLE = "bg-zinc-500/10 text-zinc-300 ring-zinc-500/25";

export function statusStyle(status: string): string {
  return STYLES[status.toUpperCase()] ?? UNKNOWN_STYLE;
}

export const statusColor = statusStyle;

export function statusLabel(status: string): string {
  return status.replaceAll("_", " ");
}

/** Ordering used by the status filter so the common outcomes come first. */
export const STATUS_ORDER = [
  "SUBMITTED",
  "ALREADY_APPLIED",
  "SKIPPED",
  "CLOSED",
  "LOGIN_REQUIRED",
  "FAILED_VALIDATION",
  "FAILED",
  "ERROR",
] as const;

export function sortStatuses(values: string[]): string[] {
  const rank = (value: string) => {
    const i = (STATUS_ORDER as readonly string[]).indexOf(value);
    return i === -1 ? STATUS_ORDER.length : i;
  };
  return [...values].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}
