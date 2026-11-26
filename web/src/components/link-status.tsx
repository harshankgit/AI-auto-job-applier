"use client";

import { useCallback, useEffect, useState } from "react";
import { KEY_STORAGE, getKey, isRemoteHost } from "@/lib/remote-fetch";

type Link = "checking" | "local" | "live" | "snapshot" | "offline" | "badkey" | "cloud";

const LABEL: Record<Link, string> = {
  checking: "Checking…",
  local: "Live · this Mac",
  live: "Live · Mac connected",
  snapshot: "Snapshot · connect Mac",
  offline: "Mac offline · cloud idle",
  cloud: "Live · cloud runner",
  badkey: "Wrong access key",
};

const TONE: Record<Link, string> = {
  checking: "bg-zinc-800/60 text-zinc-400 ring-zinc-700",
  local: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/40",
  live: "bg-emerald-500/10 text-emerald-300 ring-emerald-500/40",
  snapshot: "bg-zinc-800/60 text-zinc-300 ring-zinc-600",
  offline: "bg-amber-500/10 text-amber-300 ring-amber-500/40",
  badkey: "bg-red-500/10 text-red-300 ring-red-500/40",
  cloud: "bg-sky-500/10 text-sky-300 ring-sky-500/40",
};

export function LinkStatus() {
  const [link, setLink] = useState<Link>("checking");
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");

  const check = useCallback(async () => {
    try {
      const res = await fetch("/api/health", { cache: "no-store" });
      const header = res.headers.get("x-jobbot-link");
      const body = (await res.json().catch(() => ({}))) as { root?: boolean };
      if (header === "live") setLink("live");
      else if (header === "badkey") setLink("badkey");
      else if (header === "cloud") setLink("cloud");
      else if (header === "offline") setLink("offline");
      else if (body.root && !isRemoteHost()) setLink("local");
      else if (body.root) setLink("live");
      else setLink("snapshot");
    } catch {
      setLink("offline");
    }
  }, []);

  useEffect(() => {
    void check();
    const t = setInterval(() => void check(), 20_000);
    return () => clearInterval(t);
  }, [check]);

  const save = () => {
    const v = draft.trim();
    if (v) window.localStorage.setItem(KEY_STORAGE, v);
    else window.localStorage.removeItem(KEY_STORAGE);
    window.location.reload();
  };

  return (
    <div className="relative ml-2 shrink-0">
      <button
        type="button"
        aria-label="Mac connection"
        onClick={() => {
          setDraft(getKey());
          setOpen((o) => !o);
        }}
        className={`rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${TONE[link]}`}
      >
        <span className="mr-1">●</span>
        {LABEL[link]}
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-2 w-72 rounded-lg border border-zinc-700 bg-zinc-900 p-3 text-sm text-zinc-200 shadow-lg">
          <p className="mb-2 text-zinc-300">
            Enter the access key to control the bot on the Mac from here. It is stored only in this browser.
          </p>
          <input
            type="password"
            aria-label="Access key"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && save()}
            placeholder="Access key"
            className="mb-2 w-full rounded border border-zinc-600 bg-zinc-950 px-2 py-1.5 text-zinc-100"
          />
          <div className="flex gap-2">
            <button type="button" onClick={save} className="rounded bg-emerald-600 px-3 py-1.5 text-white">
              Connect
            </button>
            <button
              type="button"
              onClick={() => {
                window.localStorage.removeItem(KEY_STORAGE);
                window.location.reload();
              }}
              className="rounded border border-zinc-600 px-3 py-1.5"
            >
              Forget key
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
