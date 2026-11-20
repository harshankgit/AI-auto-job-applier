export const DASH = "—";

const pad = (value: number) => String(value).padStart(2, "0");

/** Deterministic UTC rendering so server and client markup always agree. */
export function formatUtc(iso: string | null | undefined, withSeconds = false): string {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return DASH;
  const base =
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    ` ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  return withSeconds ? `${base}:${pad(date.getUTCSeconds())}` : base;
}

export function formatUtcShort(iso: string | null | undefined): string {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return DASH;
  return `${pad(date.getUTCDate())} ${date.toLocaleString("en-US", { month: "short", timeZone: "UTC" })} ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
}

/** Only ever called from client components, after mount. */
export function relativeFromNow(iso: string | null | undefined, now = Date.now()): string | null {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const seconds = Math.round((now - then) / 1000);
  const future = seconds < 0;
  const abs = Math.abs(seconds);
  const unit =
    abs < 60
      ? `${abs}s`
      : abs < 3600
        ? `${Math.floor(abs / 60)}m`
        : abs < 86_400
          ? `${Math.floor(abs / 3600)}h`
          : `${Math.floor(abs / 86_400)}d`;
  return future ? `in ${unit}` : `${unit} ago`;
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return value.toLocaleString("en-US");
}

export function formatScore(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return value.toFixed(1);
}

export function text(value: string | null | undefined): string {
  const out = value?.trim();
  return out ? out : DASH;
}

export function basename(filePath: string | null | undefined): string {
  const out = filePath?.trim();
  if (!out) return DASH;
  const parts = out.split(/[\\/]/).filter(Boolean);
  return parts.length > 0 ? (parts[parts.length - 1] as string) : out;
}

export function hostOf(url: string | null | undefined): string {
  if (!url) return DASH;
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, Math.max(0, max - 1))}…` : value;
}
