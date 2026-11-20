"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { ErrorNote, Spinner } from "@/components/ui";

type Row = {
  company: string;
  title: string;
  location: string;
  status: string;
  applied_at: string;
  url: string;
  source: string;
  score: number;
  category?: string;
  why?: string;
  blocking?: string[];
  company_later_succeeded?: boolean;
};
type Question = { question: string; count: number; companies: string[]; learned: boolean; maybe_not_eligible: boolean };
type CandidateReport = {
  submitted: number;
  submitted_companies: number;
  failed: number;
  reasons: Record<string, number>;
  successes: Row[];
  failures: Row[];
  questions: Question[];
  form_control_issues?: { question: string; count: number }[];
};
type Payload = { generated_at: string | null; updatedAt?: string; candidates: Record<string, CandidateReport>; error?: string };

const CANDS = [
  { id: "harshank", label: "Harshank" },
  { id: "divya", label: "Divya" },
] as const;

const REASON_LABEL: Record<string, string> = {
  needs_answer: "Needs your answer",
  captcha: "Captcha / robot check",
  form_rejected: "Form rejected",
  unconfirmed: "No confirmation (OTP / email code)",
  closed: "Job closed",
  error: "Site error",
  resume: "Resume upload failed",
  other: "Other",
};

const DETAIL_FIELDS = [
  { key: "github", label: "GitHub URL", placeholder: "https://github.com/..." },
  { key: "linkedin", label: "LinkedIn URL", placeholder: "https://linkedin.com/in/..." },
  { key: "portfolio", label: "Portfolio URL", placeholder: "https://..." },
  { key: "current_ctc", label: "Current CTC (private)", placeholder: "e.g. 10 LPA" },
  { key: "expected_ctc", label: "Expected CTC", placeholder: "e.g. 14-15 LPA" },
  { key: "notice_period", label: "Notice period", placeholder: "e.g. 30 days" },
  { key: "work_authorized_countries", label: "Authorised to work in (comma list)", placeholder: "e.g. India, United States" },
] as const;

function fmt(ts?: string | null) {
  if (!ts) return "-";
  const d = new Date(ts.includes("T") ? ts : `${ts.replace(" ", "T")}Z`);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
}

export default function ResultsPage() {
  const [data, setData] = useState<Payload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [cid, setCid] = useState<(typeof CANDS)[number]["id"]>("harshank");
  const [tab, setTab] = useState<"questions" | "failed" | "success" | "details">("questions");

  const load = useCallback(async () => {
    try {
      const r = await fetch("/api/failures", { cache: "no-store" });
      setData((await r.json()) as Payload);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load results");
    }
  }, []);

  useEffect(() => {
    void load();
    const t = setInterval(load, 30_000);
    return () => clearInterval(t);
  }, [load]);

  const rep = data?.candidates?.[cid];

  return (
    <main className="space-y-6 px-1 py-4 sm:px-2 sm:py-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold tracking-tight text-zinc-100">Results &amp; learning</h1>
          <p className="mt-1 text-sm text-zinc-400">
            Every application, why it failed, and the questions the bot needs you to answer once. Answers are saved only on the
            bot machine and used on every next form. Updated {fmt(data?.updatedAt ?? data?.generated_at)}.
          </p>
        </div>
        <div className="flex rounded-lg border border-zinc-800 bg-zinc-900/60 p-1">
          {CANDS.map((c) => (
            <button
              key={c.id}
              onClick={() => setCid(c.id)}
              className={`rounded-md px-4 py-1.5 text-sm font-medium transition ${cid === c.id ? "bg-emerald-600 text-white" : "text-zinc-300 hover:bg-zinc-800"}`}
            >
              {c.label}
            </button>
          ))}
        </div>
      </div>

      {error && <ErrorNote title="Something went wrong" body={error} />}
      {!data && !error && <Spinner />}
      {data && !rep && <p className="text-sm text-zinc-400">{data.error ?? "No data for this candidate yet."}</p>}

      {rep && (
        <>
          <section className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label="Submitted" value={rep.submitted} tone="emerald" />
            <Stat label="Companies (distinct)" value={rep.submitted_companies} tone="emerald" />
            <Stat label="Failed attempts" value={rep.failed} tone="rose" />
            <Stat label="Open questions" value={rep.questions.filter((q) => !q.learned).length} tone="amber" />
          </section>

          <section className="flex flex-wrap gap-2">
            {Object.entries(rep.reasons)
              .sort((a, b) => b[1] - a[1])
              .map(([k, v]) => (
                <span key={k} className="rounded-full border border-zinc-800 bg-zinc-900/60 px-3 py-1 text-xs text-zinc-300">
                  {REASON_LABEL[k] ?? k}: <b className="text-zinc-100">{v}</b>
                </span>
              ))}
          </section>

          <div className="-mx-1 flex gap-1 overflow-x-auto whitespace-nowrap border-b border-zinc-800 px-1">
            {(
              [
                ["questions", "Questions to answer"],
                ["failed", `Failed (${rep.failures.length})`],
                ["success", `Submitted (${rep.successes.length})`],
                ["details", "My details"],
              ] as const
            ).map(([k, l]) => (
              <button
                key={k}
                onClick={() => setTab(k)}
                className={`-mb-px border-b-2 px-3 py-2 text-sm ${tab === k ? "border-emerald-500 text-zinc-100" : "border-transparent text-zinc-400 hover:text-zinc-200"}`}
              >
                {l}
              </button>
            ))}
          </div>

          {tab === "questions" && <Questions cid={cid} questions={rep.questions} onSaved={load} />}
          {tab === "failed" && <Rows rows={rep.failures} failed />}
          {tab === "success" && <Rows rows={rep.successes} />}
          {tab === "details" && <Details cid={cid} />}
        </>
      )}
    </main>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: "emerald" | "rose" | "amber" }) {
  const c = tone === "emerald" ? "text-emerald-400" : tone === "rose" ? "text-rose-400" : "text-amber-400";
  return (
    <div className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-4">
      <div className="text-xs uppercase tracking-wide text-zinc-500">{label}</div>
      <div className={`mt-1 text-2xl font-bold ${c}`}>{value}</div>
    </div>
  );
}

function Questions({ cid, questions, onSaved }: { cid: string; questions: Question[]; onSaved: () => void }) {
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [scopeBoth, setScopeBoth] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [showAll, setShowAll] = useState(false);

  const list = useMemo(() => {
    const open = questions.filter((q) => !q.learned && q.question.length > 3);
    return showAll ? open : open.slice(0, 40);
  }, [questions, showAll]);

  const save = async () => {
    const answers = Object.entries(draft)
      .filter(([, a]) => a.trim())
      .map(([question, answer]) => ({ question, answer }));
    if (!answers.length) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await fetch("/api/learn", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ candidate: scopeBoth ? "both" : cid, answers }),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error ?? "save failed");
      setMsg(`Saved ${j.saved} answer(s). The bot uses them on the very next form and retries those companies.`);
      setDraft({});
      onSaved();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "save failed");
    } finally {
      setBusy(false);
    }
  };

  if (!list.length) return <p className="text-sm text-zinc-400">No open questions - everything the forms asked is answered.</p>;

  return (
    <section className="space-y-3">
      <p className="text-sm text-zinc-400">
        These required questions blocked applications because the resume does not answer them. Type a truthful answer (for
        Yes/No questions just type Yes or No). Leave blank to skip. Items marked <span className="text-amber-400">eligibility</span>{" "}
        are often a real mismatch (years / work permit) - answer only if it is true.
      </p>
      <div className="space-y-2">
        {list.map((q) => (
          <div key={q.question} className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-3 sm:flex sm:items-start sm:gap-4">
            <div className="min-w-0 flex-1">
              <div className="break-words text-sm font-medium text-zinc-100">{q.question}</div>
              <div className="mt-1 text-xs text-zinc-500">
                blocked {q.count}x - {q.companies.slice(0, 4).join(", ")}
                {q.companies.length > 4 ? "..." : ""}
                {q.maybe_not_eligible && <span className="ml-2 text-amber-400">eligibility</span>}
              </div>
            </div>
            <input
              value={draft[q.question] ?? ""}
              onChange={(e) => setDraft((d) => ({ ...d, [q.question]: e.target.value }))}
              placeholder="Your answer"
              className="mt-2 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-500 sm:mt-0 sm:w-72"
            />
          </div>
        ))}
      </div>
      <div className="sticky bottom-2 flex flex-wrap items-center gap-3 rounded-xl border border-zinc-800 bg-zinc-950/95 p-3 backdrop-blur">
        <button
          onClick={save}
          disabled={busy || !Object.values(draft).some((v) => v.trim())}
          className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
        >
          {busy ? "Saving..." : "Save answers"}
        </button>
        <label className="flex items-center gap-2 text-sm text-zinc-300">
          <input type="checkbox" checked={scopeBoth} onChange={(e) => setScopeBoth(e.target.checked)} /> use for both candidates
        </label>
        {questions.filter((q) => !q.learned).length > 40 && (
          <button onClick={() => setShowAll((s) => !s)} className="text-sm text-zinc-400 underline">
            {showAll ? "show fewer" : "show all"}
          </button>
        )}
        {msg && <span className="text-sm text-zinc-300">{msg}</span>}
      </div>
    </section>
  );
}

function Rows({ rows, failed }: { rows: Row[]; failed?: boolean }) {
  const [q, setQ] = useState("");
  const shown = rows.filter((r) => `${r.company} ${r.title} ${r.location} ${r.why ?? ""}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <section className="space-y-3">
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="Filter by company, role, city, reason..."
        className="w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-emerald-500"
      />
      <div className="grid gap-2 md:grid-cols-2">
        {shown.slice(0, 300).map((r, i) => (
          <div key={`${r.url}-${i}`} className="rounded-xl border border-zinc-800 bg-zinc-900/50 p-3">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="truncate text-sm font-semibold text-zinc-100">{r.company}</div>
                <div className="truncate text-sm text-zinc-300">{r.title}</div>
              </div>
              <span
                className={`shrink-0 rounded-full px-2 py-0.5 text-[11px] font-medium ${failed ? "bg-rose-500/15 text-rose-300" : "bg-emerald-500/15 text-emerald-300"}`}
              >
                {failed ? (REASON_LABEL[r.category ?? ""] ?? r.status) : "Submitted"}
              </span>
            </div>
            <div className="mt-1 text-xs text-zinc-500">
              {r.location || "-"} - {r.source} - score {Math.round(r.score)} - {fmt(r.applied_at)}
            </div>
            {failed && r.why && <div className="mt-2 text-xs text-zinc-300">{r.why}</div>}
            {failed && r.blocking && r.blocking.length > 0 && (
              <ul className="mt-1 list-disc pl-4 text-xs text-amber-300/90">
                {r.blocking.slice(0, 4).map((b) => (
                  <li key={b} className="break-words">
                    {b}
                  </li>
                ))}
              </ul>
            )}
            {failed && r.company_later_succeeded && <div className="mt-1 text-xs text-emerald-400">Applied there successfully later</div>}
            {r.url && (
              <a href={r.url} target="_blank" rel="noreferrer" className="mt-2 inline-block text-xs text-emerald-400 hover:underline">
                Open job posting
              </a>
            )}
          </div>
        ))}
      </div>
      {shown.length === 0 && <p className="text-sm text-zinc-400">Nothing here yet.</p>}
    </section>
  );
}

function Details({ cid }: { cid: string }) {
  const [vals, setVals] = useState<Record<string, string>>({});
  const [readOnly, setReadOnly] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    setMsg(null);
    fetch(`/api/profile-details?candidate=${cid}`, { cache: "no-store" })
      .then((r) => r.json())
      .then((j) => {
        setVals(j.details ?? {});
        setReadOnly(Boolean(j.readOnly));
      })
      .catch(() => setReadOnly(true));
  }, [cid]);

  const save = async () => {
    const r = await fetch("/api/profile-details", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ candidate: cid, details: vals }),
    });
    const j = await r.json();
    setMsg(r.ok ? "Saved privately on the bot machine (never committed or published)." : (j.error ?? "save failed"));
  };

  return (
    <section className="space-y-3">
      <p className="text-sm text-zinc-400">
        Details the bot may need on forms. Stored only in <code>data/private_profile.json</code> on the bot Mac - not in git, not
        on the public site.
      </p>
      {readOnly && <p className="text-sm text-amber-400">Read-only here: open the dashboard on the bot machine to edit.</p>}
      <div className="grid gap-3 sm:grid-cols-2">
        {DETAIL_FIELDS.map((f) => (
          <label key={f.key} className="block text-sm">
            <span className="text-zinc-300">{f.label}</span>
            <input
              disabled={readOnly}
              value={vals[f.key] ?? ""}
              onChange={(e) => setVals((v) => ({ ...v, [f.key]: e.target.value }))}
              placeholder={f.placeholder}
              className="mt-1 w-full rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-zinc-100 outline-none focus:border-emerald-500 disabled:opacity-50"
            />
          </label>
        ))}
      </div>
      <button onClick={save} disabled={readOnly} className="rounded-lg bg-emerald-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-40">
        Save details
      </button>
      {msg && <p className="text-sm text-zinc-300">{msg}</p>}
    </section>
  );
}
