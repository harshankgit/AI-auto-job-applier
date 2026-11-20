"use client";

import { useRef, useState } from "react";

import { Spinner } from "@/components/ui";

const ALLOWED = [".pdf", ".doc", ".docx"];
const MAX_BYTES = 10 * 1024 * 1024;

type Phase = "idle" | "uploading" | "done" | "error";

function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot === -1 ? "" : name.slice(dot).toLowerCase();
}

export function ResumeDrop({
  candidate,
  onUploaded,
}: {
  candidate: string;
  onUploaded: (path: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>("idle");
  const [message, setMessage] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);

  const upload = async (file: File) => {
    const extension = extensionOf(file.name);
    if (!ALLOWED.includes(extension)) {
      setPhase("error");
      setMessage(`${extension || "that file type"} is not accepted — use PDF, DOC or DOCX`);
      return;
    }
    if (file.size > MAX_BYTES) {
      setPhase("error");
      setMessage(`${(file.size / (1024 * 1024)).toFixed(1)} MB exceeds the 10 MB limit`);
      return;
    }

    setPhase("uploading");
    setMessage(file.name);

    try {
      const body = new FormData();
      body.set("candidate", candidate);
      body.set("file", file);
      const response = await fetch("/api/resume", { method: "POST", body });
      const payload = (await response.json()) as { path?: string; error?: string };

      if (!response.ok || !payload.path) {
        setPhase("error");
        setMessage(payload.error ?? `upload failed (HTTP ${response.status})`);
        return;
      }
      setPhase("done");
      setMessage("profiles.yaml now points at this file");
      onUploaded(payload.path);
    } catch (err) {
      setPhase("error");
      setMessage(err instanceof Error ? err.message : "upload failed");
    }
  };

  const busy = phase === "uploading";

  return (
    <div>
      <div
        onDragOver={(event) => {
          event.preventDefault();
          if (!busy) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (busy) return;
          const file = event.dataTransfer.files?.[0];
          if (file) void upload(file);
        }}
        className={[
          "rounded-lg border border-dashed px-3 py-3 transition-colors",
          dragging ? "border-indigo-500/70 bg-indigo-500/5" : "border-zinc-700/80 bg-zinc-900/30",
          busy ? "opacity-60" : "",
        ].join(" ")}
      >
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
          <button
            type="button"
            disabled={busy}
            onClick={() => inputRef.current?.click()}
            className="btn-ghost h-8 px-2.5 py-0 text-xs"
          >
            {busy ? <Spinner /> : null}
            {busy ? "Uploading" : "Replace resume"}
          </button>
          <span className="text-[11px] text-zinc-500">
            or drop a PDF / DOC / DOCX here · max 10 MB
          </span>
        </div>

        <input
          ref={inputRef}
          type="file"
          accept=".pdf,.doc,.docx,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
          className="hidden"
          onChange={(event) => {
            const file = event.target.files?.[0];
            event.target.value = "";
            if (file) void upload(file);
          }}
        />
      </div>

      {message ? (
        <p
          className={[
            "mt-1.5 text-[11px] break-words",
            phase === "error"
              ? "text-rose-300"
              : phase === "done"
                ? "text-emerald-300"
                : "text-zinc-500",
          ].join(" ")}
        >
          {message}
        </p>
      ) : null}
    </div>
  );
}
