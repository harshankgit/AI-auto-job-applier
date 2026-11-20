import { statusStyle } from "@/lib/status";

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.trim().toUpperCase() || "UNKNOWN";
  return (
    <span className={`pill ${statusStyle(normalized)}`}>{normalized.replaceAll("_", " ")}</span>
  );
}

export function StatCard({
  label,
  value,
  hint,
  tone = "default",
}: {
  label: string;
  value: string;
  hint?: string | undefined;
  tone?: "default" | "good" | "bad";
}) {
  const valueTone =
    tone === "good" ? "text-emerald-300" : tone === "bad" ? "text-rose-300" : "text-zinc-100";
  return (
    <div className="card px-4 py-3.5">
      <div className="label-caps">{label}</div>
      <div className={`num mt-2 text-2xl leading-none font-semibold tracking-tight ${valueTone}`}>
        {value}
      </div>
      {hint ? <div className="mt-1.5 truncate text-xs text-zinc-500">{hint}</div> : null}
    </div>
  );
}

export function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="label-caps">{label}</div>
      <div className="num mt-1 text-lg leading-none font-semibold text-zinc-100">{value}</div>
    </div>
  );
}

export function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0">
      <div className="label-caps">{label}</div>
      <div className="mt-1 truncate text-[13px] text-zinc-300" title={value}>
        {value}
      </div>
    </div>
  );
}

export function EmptyState({ title, body }: { title: string; body?: string }) {
  return (
    <div className="flex flex-col items-center justify-center gap-1.5 px-6 py-12 text-center">
      <p className="text-sm font-medium text-zinc-300">{title}</p>
      {body ? <p className="max-w-md text-xs leading-relaxed text-zinc-500">{body}</p> : null}
    </div>
  );
}

export function ErrorNote({ title, body }: { title: string; body?: string | null }) {
  return (
    <div className="rounded-lg border border-rose-900/60 bg-rose-950/30 px-4 py-3">
      <p className="text-sm font-medium text-rose-200">{title}</p>
      {body ? <p className="mt-1 font-mono text-xs break-words text-rose-300/80">{body}</p> : null}
    </div>
  );
}

export function Spinner({ className = "" }: { className?: string }) {
  return (
    <span
      aria-hidden
      className={`inline-block h-3.5 w-3.5 animate-spin rounded-full border-[1.5px] border-current border-t-transparent ${className}`}
    />
  );
}

export function SkeletonRows({ rows = 4, cols = 6 }: { rows?: number; cols?: number }) {
  return (
    <div className="animate-pulse space-y-2 p-4">
      {Array.from({ length: rows }).map((_, rowIndex) => (
        <div key={rowIndex} className="flex gap-3">
          {Array.from({ length: cols }).map((_, colIndex) => (
            <div
              key={colIndex}
              className="h-3.5 flex-1 rounded bg-zinc-800/70"
              style={{ maxWidth: colIndex === 0 ? "7rem" : undefined }}
            />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SourceTag({ source }: { source: "sqlite" | "csv" | "none" }) {
  const copy =
    source === "sqlite"
      ? "data/applications.db"
      : source === "csv"
        ? "reports/applications.csv"
        : "no data source found";
  return (
    <span className="pill bg-zinc-800/60 text-zinc-400 ring-zinc-700/60">
      <span className="font-mono">{copy}</span>
    </span>
  );
}
