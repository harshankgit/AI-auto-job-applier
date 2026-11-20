"use client";

import { useEffect, useState } from "react";

import { ApplicationsTable } from "@/components/applications-table";
import type { ApplicationsPayload } from "@/lib/types";

export default function ApplicationsPage() {
  const [payload, setPayload] = useState<ApplicationsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const load = async () => {
      try {
        const res = await fetch("/api/applications", { cache: "no-store" });
        const data = (await res.json()) as ApplicationsPayload;
        setPayload(data);
        setError(null);
      } catch (err) {
        setError(err instanceof Error ? err.message : "failed to load applications");
      } finally {
        setLoading(false);
      }
    };

    void load();
  }, []);

  return (
    <main className="space-y-8 px-4 py-8 sm:px-6 lg:px-8">
      <div>
        <h1 className="mb-4 text-2xl font-bold tracking-tight text-zinc-100">Applications</h1>
        <p className="text-sm text-zinc-400">
          View and filter all job applications. Sort by date, score, or company. Export to CSV.
        </p>
      </div>
      <ApplicationsTable payload={payload} loading={loading} error={error} />
    </main>
  );
}
