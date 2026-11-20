"use client";

import { ResumeDrop } from "@/components/resume-drop";
import { EmptyState, Field, MiniStat, SkeletonRows, Spinner, StatusBadge } from "@/components/ui";
import { accentFor } from "@/lib/candidates";
import { basename, DASH, formatNumber, formatScore, formatUtc, text } from "@/lib/format";
import type { Application, CandidateMeta, CandidateStats } from "@/lib/types";

const RECENT_LIMIT = 10;

export function CandidateCard({
  meta,
  stats,
  rows,
  resumePath,
  loading,
  blocked,
  activeHere,
  onRun,
  onResumeUploaded,
}: {
  meta: CandidateMeta;
  stats: CandidateStats | undefined;
  rows: Application[];
  resumePath: string | null;
  loading: boolean;
  blocked: boolean;
  activeHere: boolean;
  onRun: () => void;
  onResumeUploaded: (path: string) => void;
}) {
  const accent = accentFor(meta.id);
  const recent = rows.slice(0, RECENT_LIMIT);

  // Contact details fall back to what the bot actually typed into forms.
  const email = meta.email ?? rows.find((row) => row.emailUsed)?.emailUsed ?? null;
  const phone = meta.phone ?? rows.find((row) => row.phoneUsed)?.phoneUsed ?? null;

  const resumeInUse = resumePath ?? meta.defaultResume;
  const usingFallback = resumePath === null;

  return (
    <section className={`card overflow-hidden border-l-2 ${accent.ring} border-l-current`}>
      <div className={accent.text}>
        <header className="flex flex-wrap items-start gap-x-4 gap-y-3 border-b border-zinc-800 px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span aria-hidden className={`h-1.5 w-1.5 rounded-full ${accent.dot}`} />
              <h2 className="truncate text-[15px] font-semibold tracking-tight text-zinc-100">
                {meta.name}
              </h2>
              <span className={`pill ${accent.soft}`}>{meta.id}</span>
            </div>
            <p className="mt-1 text-[13px] text-zinc-400">
              {meta.title} · {meta.yearsExperience} {meta.yearsExperience === 1 ? "yr" : "yrs"}{" "}
              experience · expects {meta.expectedCtc}
            </p>
          </div>

          <button
            type="button"
            onClick={onRun}
            disabled={blocked}
            title={
              blocked
                ? "A run is already active — only one Chrome automation at a time"
                : `Discover and apply to the latest matching jobs for ${meta.name}`
            }
            className={`btn w-full sm:w-auto ${accent.button}`}
          >
            {activeHere ? <Spinner /> : null}
            {activeHere ? "Running…" : "Find & Apply to Latest Jobs"}
          </button>
        </header>

        <div className="grid grid-cols-2 gap-x-4 gap-y-3.5 border-b border-zinc-800 px-5 py-4 lg:grid-cols-3">
          <Field label="Email" value={text(email)} />
          <Field label="Phone" value={text(phone)} />
          <Field label="Location" value={meta.location} />
          <div className="col-span-2 min-w-0 lg:col-span-3">
            <div className="label-caps">Resume in use</div>
            <div className="mt-1 flex flex-wrap items-baseline gap-2">
              <span className="truncate font-mono text-[12.5px] text-zinc-200" title={resumeInUse}>
                {basename(resumeInUse)}
              </span>
              {usingFallback ? (
                <span className="text-[11px] text-amber-300/80">
                  (config/profiles.yaml unreadable — showing the configured default)
                </span>
              ) : null}
            </div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4 border-b border-zinc-800 px-5 py-4 sm:grid-cols-4">
          <MiniStat label="Attempts" value={formatNumber(stats?.total ?? 0)} />
          <MiniStat label="Submitted" value={formatNumber(stats?.submitted ?? 0)} />
          <MiniStat label="Companies" value={formatNumber(stats?.companies ?? 0)} />
          <MiniStat label="Avg score" value={formatScore(stats?.avgScore ?? null)} />
        </div>

        <div className="border-b border-zinc-800 px-5 py-4">
          <ResumeDrop candidate={meta.id} onUploaded={onResumeUploaded} />
        </div>

        <div>
          <div className="flex items-center justify-between px-5 pt-4 pb-2">
            <h3 className="label-caps">Recent applications</h3>
            <span className="num text-[11px] text-zinc-500">
              {recent.length > 0 ? `${recent.length} of ${formatNumber(rows.length)}` : ""}
            </span>
          </div>

          {loading && rows.length === 0 ? (
            <SkeletonRows rows={4} cols={5} />
          ) : recent.length === 0 ? (
            <EmptyState
              title="No applications recorded yet"
              body={`Click "Find & Apply to Latest Jobs" to start a run for ${meta.name}.`}
            />
          ) : (
            <div className="scroll-thin overflow-x-auto">
              <table className="w-full min-w-[640px] border-collapse text-[13px]">
                <thead>
                  <tr className="border-y border-zinc-800 bg-zinc-900/60 text-left">
                    <th className="label-caps px-5 py-2 font-semibold">Applied</th>
                    <th className="label-caps px-3 py-2 font-semibold">Company</th>
                    <th className="label-caps px-3 py-2 font-semibold">Job title</th>
                    <th className="label-caps px-3 py-2 font-semibold">Location</th>
                    <th className="label-caps px-3 py-2 text-right font-semibold">Score</th>
                    <th className="label-caps px-3 py-2 font-semibold">Status</th>
                    <th className="label-caps px-5 py-2 text-right font-semibold">Link</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.map((row, index) => (
                    <tr
                      key={row.id ?? `${row.jobKey ?? "row"}-${index}`}
                      className="border-b border-zinc-800/60 last:border-0 hover:bg-zinc-900/40"
                    >
                      <td className="num px-5 py-2.5 whitespace-nowrap text-zinc-400">
                        {formatUtc(row.appliedAt)}
                      </td>
                      <td className="px-3 py-2.5 font-medium whitespace-nowrap text-zinc-200">
                        {text(row.company)}
                      </td>
                      <td className="max-w-[260px] truncate px-3 py-2.5 text-zinc-300">
                        <span title={row.title ?? undefined}>{text(row.title)}</span>
                      </td>
                      <td className="max-w-[160px] truncate px-3 py-2.5 text-zinc-400">
                        <span title={row.jobLocation ?? undefined}>{text(row.jobLocation)}</span>
                      </td>
                      <td className="num px-3 py-2.5 text-right text-zinc-300">
                        {formatScore(row.score)}
                      </td>
                      <td className="px-3 py-2.5">
                        <StatusBadge status={row.status} />
                      </td>
                      <td className="px-5 py-2.5 text-right whitespace-nowrap">
                        {row.applyUrl ? (
                          <a
                            href={row.applyUrl}
                            target="_blank"
                            rel="noreferrer noopener"
                            className="link text-[12px]"
                            title={row.applyUrl}
                          >
                            Open
                          </a>
                        ) : (
                          <span className="text-zinc-600">{DASH}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
