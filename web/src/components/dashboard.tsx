"use client";

import { useCallback, useEffect, useState } from "react";

import { CandidateCard } from "@/components/candidate-card";
import { LogPanel } from "@/components/log-panel";
import { ErrorNote, Spinner, StatCard } from "@/components/ui";
import { candidateIds, candidateMeta } from "@/lib/candidates";
import { formatUtc } from "@/lib/format";
import type { Application, ApplicationsPayload, RunStatus } from "@/lib/types";

type LoadState = "loading" | "ready" | "error";

export function Dashboard() {
  const [loadState, setLoadState] = useState<LoadState>("loading");
  const [payload, setPayload] = useState<ApplicationsPayload | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [resumePaths, setResumePaths] = useState<Record<string, string | null>>({});
  const [error, setError] = useState<string | null>(null);

  // Load applications and status on mount, then poll status every 2s.
  useEffect(() => {
    const load = async () => {
      try {
        const [appRes, statusRes] = await Promise.all([
          fetch("/api/applications", { cache: "no-store" }),
          fetch("/api/status", { cache: "no-store" }),
        ]);
        const appData = (await appRes.json()) as ApplicationsPayload;
        const statusData = (await statusRes.json()) as RunStatus;
        setPayload(appData);
        setStatus(statusData);
        setLoadState("ready");
        setError(null);
      } catch (err) {
        setLoadState("error");
        setError(err instanceof Error ? err.message : "failed to load data");
      }
    };

    void load();
    const interval = setInterval(() => void load(), 2000);
    return () => clearInterval(interval);
  }, []);

  const onRun = useCallback(
    async (candidate: string) => {
      if (!status || status.running) return;
      try {
        const res = await fetch("/api/apply", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ candidate, limit: 40 }),
        });
        const data = (await res.json()) as { started?: boolean; error?: string };
        if (!res.ok || !data.started) {
          setError(data.error ?? `failed to start run (HTTP ${res.status})`);
          return;
        }
        setShowLog(true);
        setStatus((prev) => (prev ? { ...prev, running: true, candidate } : null));
      } catch (err) {
        setError(err instanceof Error ? err.message : "failed to start run");
      }
    },
    [status],
  );

  const onResumeUploaded = useCallback((candidate: string, path: string) => {
    setResumePaths((prev) => ({ ...prev, [candidate]: path }));
  }, []);

  const onRunEnd = useCallback(() => {
    setStatus((prev) => (prev ? { ...prev, running: false } : null));
    // Reload applications to show new entries.
    void (async () => {
      try {
        const res = await fetch("/api/applications", { cache: "no-store" });
        const data = (await res.json()) as ApplicationsPayload;
        setPayload(data);
      } catch {
        /* silently fail; user can refresh */
      }
    })();
  }, []);

  const blocked = status?.running ?? false;
  const externalRun = status?.running && status.source === "external";

  const stats = payload?.stats;
  const allRows = payload?.rows ?? [];

  return (
    <div className="space-y-6">
      {/* Header with global stats */}
      <section>
        <h1 className="mb-4 text-2xl font-bold tracking-tight text-zinc-100">Dashboard</h1>
        {loadState === "loading" ? (
          <div className="flex items-center gap-2 text-sm text-zinc-400">
            <Spinner />
            Loading applications…
          </div>
        ) : loadState === "error" ? (
          <ErrorNote title="Failed to load data" body={error} />
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
            <StatCard label="Total" value={String(stats?.total ?? 0)} />
            <StatCard label="Submitted" value={String(stats?.submitted ?? 0)} tone="good" />
            <StatCard label="Skipped" value={String(stats?.skipped ?? 0)} />
            <StatCard label="Failed" value={String(stats?.failed ?? 0)} tone="bad" />
            <StatCard label="Companies" value={String(stats?.companies ?? 0)} />
            <StatCard label="Avg Score" value={stats?.avgScore ? stats.avgScore.toFixed(1) : "—"} />
          </div>
        )}
      </section>

      {/* External run banner */}
      {externalRun ? (
        <div className="rounded-lg border border-amber-900/60 bg-amber-950/30 px-4 py-3">
          <p className="text-sm font-medium text-amber-200">
            A run started from a terminal is currently active. Dashboard triggers are disabled.
          </p>
        </div>
      ) : null}

      {/* Error banner */}
      {error ? <ErrorNote title="Error" body={error} /> : null}

      {/* Candidate cards */}
      {loadState === "ready" && payload ? (
        <div className="space-y-6">
          {candidateIds.map((candidateId) => {
            const meta = candidateMeta(candidateId);
            if (!meta) return null;
            const rows = allRows.filter((row) => row.candidateId === candidateId);
            const stats = payload.stats.byCandidate[candidateId];
            const resumePath = resumePaths[candidateId] ?? null;
            const activeHere = status?.running && status.candidate === candidateId;

            return (
              <CandidateCard
                key={candidateId}
                meta={meta}
                stats={stats}
                rows={rows}
                resumePath={resumePath}
                loading={false}
                blocked={blocked}
                activeHere={activeHere ?? false}
                onRun={() => onRun(candidateId)}
                onResumeUploaded={(path) => onResumeUploaded(candidateId, path)}
              />
            );
          })}
        </div>
      ) : null}

      {/* Log panel */}
      {showLog ? (
        <LogPanel
          running={status?.running ?? false}
          onRunEnd={onRunEnd}
          onClose={() => setShowLog(false)}
        />
      ) : null}
    </div>
  );
}
