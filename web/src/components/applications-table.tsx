"use client";

import { useMemo, useState } from "react";

import { ErrorNote, Spinner } from "@/components/ui";
import type { Application, ApplicationsPayload } from "@/lib/types";
import { formatUtc } from "@/lib/format";
import { statusColor } from "@/lib/status";

type SortKey = "applied" | "score" | "company" | "candidate" | "status";
type SortDir = "asc" | "desc";

interface ApplicationsTableProps {
  payload: ApplicationsPayload | null;
  loading: boolean;
  error: string | null;
}

export function ApplicationsTable({ payload, loading, error }: ApplicationsTableProps) {
  const [candidateFilter, setCandidateFilter] = useState<string>("");
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [companySearch, setCompanySearch] = useState<string>("");
  const [sourceFilter, setSourceFilter] = useState<string>("");
  const [sortKey, setSortKey] = useState<SortKey>("applied");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const rows = payload?.rows ?? [];

  const filtered = useMemo(() => {
    return rows.filter((row) => {
      if (candidateFilter && row.candidateId !== candidateFilter) return false;
      if (statusFilter && row.status !== statusFilter) return false;
      if (companySearch && !row.company?.toLowerCase().includes(companySearch.toLowerCase()))
        return false;
      if (sourceFilter && row.source !== sourceFilter) return false;
      return true;
    });
  }, [rows, candidateFilter, statusFilter, companySearch, sourceFilter]);

  const sorted = useMemo(() => {
    const copy = [...filtered];
    copy.sort((a, b) => {
      let aVal: string | number | null = null;
      let bVal: string | number | null = null;

      if (sortKey === "applied") {
        aVal = a.appliedAt ?? "";
        bVal = b.appliedAt ?? "";
      } else if (sortKey === "score") {
        aVal = a.score ?? 0;
        bVal = b.score ?? 0;
      } else if (sortKey === "company") {
        aVal = a.company ?? "";
        bVal = b.company ?? "";
      } else if (sortKey === "candidate") {
        aVal = a.candidateId ?? "";
        bVal = b.candidateId ?? "";
      } else if (sortKey === "status") {
        aVal = a.status ?? "";
        bVal = b.status ?? "";
      }

      if (aVal === null || aVal === "") aVal = sortKey === "score" ? -1 : "\uffff";
      if (bVal === null || bVal === "") bVal = sortKey === "score" ? -1 : "\uffff";

      let cmp = 0;
      if (typeof aVal === "number" && typeof bVal === "number") {
        cmp = aVal - bVal;
      } else {
        cmp = String(aVal).localeCompare(String(bVal));
      }

      return sortDir === "asc" ? cmp : -cmp;
    });
    return copy;
  }, [filtered, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir(sortDir === "asc" ? "desc" : "asc");
    } else {
      setSortKey(key);
      setSortDir("desc");
    }
  };

  const exportCsv = () => {
    const headers = ["Applied", "Candidate", "Company", "Title", "Location", "Score", "Status"];
    const lines = [headers.join(",")];
    for (const row of sorted) {
      lines.push(
        [
          row.appliedAt ?? "",
          row.candidateId ?? "",
          row.company ?? "",
          row.title ?? "",
          row.jobLocation ?? "",
          row.score ?? "",
          row.status ?? "",
        ]
          .map((v) => `"${String(v).replace(/"/g, '""')}"`)
          .join(","),
      );
    }
    const csv = lines.join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `applications-${new Date().toISOString().split("T")[0]}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const uniqueCandidates = [...new Set(rows.map((r) => r.candidateId))].sort();
  const uniqueStatuses = [...new Set(rows.map((r) => r.status))].sort();
  const uniqueSources = [...new Set(rows.map((r) => r.source).filter((s): s is string => s !== null))].sort();

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-sm text-zinc-400">
        <Spinner />
        Loading applications…
      </div>
    );
  }

  if (error) {
    return <ErrorNote title="Failed to load applications" body={error} />;
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label className="block text-xs font-medium text-zinc-400">Candidate</label>
          <select
            value={candidateFilter}
            onChange={(e) => setCandidateFilter(e.target.value)}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm text-zinc-100 focus:border-indigo-500 focus:outline-none"
          >
            <option value="">All</option>
            {uniqueCandidates.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-zinc-400">Status</label>
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm text-zinc-100 focus:border-indigo-500 focus:outline-none"
          >
            <option value="">All</option>
            {uniqueStatuses.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-zinc-400">Source</label>
          <select
            value={sourceFilter}
            onChange={(e) => setSourceFilter(e.target.value)}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm text-zinc-100 focus:border-indigo-500 focus:outline-none"
          >
            <option value="">All</option>
            {uniqueSources.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label className="block text-xs font-medium text-zinc-400">Company</label>
          <input
            type="text"
            placeholder="Search…"
            value={companySearch}
            onChange={(e) => setCompanySearch(e.target.value)}
            className="mt-1 w-full rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm text-zinc-100 placeholder-zinc-500 focus:border-indigo-500 focus:outline-none"
          />
        </div>
      </div>

      {/* Export button */}
      <div className="flex items-center justify-between">
        <p className="text-sm text-zinc-400">
          {sorted.length} of {rows.length} applications
        </p>
        <button
          onClick={exportCsv}
          className="rounded bg-indigo-600 px-3 py-1 text-sm font-medium text-white hover:bg-indigo-700 focus:outline-none"
        >
          Export CSV
        </button>
      </div>

      {/* Table */}
      <div className="overflow-x-auto rounded border border-zinc-800 bg-zinc-900/50">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-800 bg-zinc-900">
              <th className="px-4 py-2 text-left font-medium text-zinc-300">
                <button
                  onClick={() => toggleSort("applied")}
                  className="flex items-center gap-1 hover:text-zinc-100"
                >
                  Applied {sortKey === "applied" && (sortDir === "asc" ? "↑" : "↓")}
                </button>
              </th>
              <th className="px-4 py-2 text-left font-medium text-zinc-300">
                <button
                  onClick={() => toggleSort("candidate")}
                  className="flex items-center gap-1 hover:text-zinc-100"
                >
                  Candidate {sortKey === "candidate" && (sortDir === "asc" ? "↑" : "↓")}
                </button>
              </th>
              <th className="px-4 py-2 text-left font-medium text-zinc-300">
                <button
                  onClick={() => toggleSort("company")}
                  className="flex items-center gap-1 hover:text-zinc-100"
                >
                  Company {sortKey === "company" && (sortDir === "asc" ? "↑" : "↓")}
                </button>
              </th>
              <th className="px-4 py-2 text-left font-medium text-zinc-300">Title</th>
              <th className="px-4 py-2 text-left font-medium text-zinc-300">Location</th>
              <th className="px-4 py-2 text-left font-medium text-zinc-300">
                <button
                  onClick={() => toggleSort("score")}
                  className="flex items-center gap-1 hover:text-zinc-100"
                >
                  Score {sortKey === "score" && (sortDir === "asc" ? "↑" : "↓")}
                </button>
              </th>
              <th className="px-4 py-2 text-left font-medium text-zinc-300">
                <button
                  onClick={() => toggleSort("status")}
                  className="flex items-center gap-1 hover:text-zinc-100"
                >
                  Status {sortKey === "status" && (sortDir === "asc" ? "↑" : "↓")}
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr>
                <td colSpan={7} className="px-4 py-8 text-center text-sm text-zinc-500">
                  No applications found.
                </td>
              </tr>
            ) : (
              sorted.map((row, idx) => (
                <tr key={idx} className="border-b border-zinc-800/50 hover:bg-zinc-800/30">
                  <td className="px-4 py-2 text-zinc-300">
                    {row.appliedAt ? formatUtc(row.appliedAt) : "—"}
                  </td>
                  <td className="px-4 py-2 text-zinc-300">{row.candidateId ?? "—"}</td>
                  <td className="px-4 py-2 text-zinc-300">{row.company ?? "—"}</td>
                  <td className="px-4 py-2 text-zinc-300">{row.title ?? "—"}</td>
                  <td className="px-4 py-2 text-zinc-300">{row.jobLocation ?? "—"}</td>
                  <td className="px-4 py-2 text-zinc-300">{row.score ?? "—"}</td>
                  <td className="px-4 py-2">
                    <span
                      className={`inline-block rounded px-2 py-1 text-xs font-medium ${statusColor(row.status)}`}
                    >
                      {row.status ?? "—"}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
