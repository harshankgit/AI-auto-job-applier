"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Spinner } from "@/components/ui";

type StreamState = "connecting" | "live" | "closed" | "error";

const MAX_LINES = 500;

function lineTone(line: string): string {
  if (line.startsWith("[dashboard]")) return "text-indigo-300/90";
  if (line.startsWith("===")) return "text-zinc-100 font-medium";
  if (/\b(ERROR|FAILED|FAILED_VALIDATION|Traceback|error:)\b/.test(line)) return "text-rose-300";
  if (/\bSUBMITTED\b/.test(line)) return "text-emerald-300";
  if (/\b(SKIPPED|CLOSED|ALREADY_APPLIED|LOGIN_REQUIRED)\b/.test(line)) return "text-amber-200/90";
  if (/^\s*\[\d+\/\d+\]/.test(line)) return "text-zinc-200";
  return "text-zinc-400";
}

export function LogPanel({
  onRunEnd,
  onClose,
  running,
}: {
  onRunEnd: () => void;
  onClose: () => void;
  running: boolean;
}) {
  const [lines, setLines] = useState<string[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const [stick, setStick] = useState(true);

  const boxRef = useRef<HTMLDivElement | null>(null);
  const stickRef = useRef(true);
  const endRef = useRef(onRunEnd);
  endRef.current = onRunEnd;
  stickRef.current = stick;

  useEffect(() => {
    let source: EventSource | null = null;
    try {
      source = new EventSource("/api/logs");
    } catch {
      setState("error");
      return;
    }
    const es = source;

    const append = (incoming: string[]) => {
      if (incoming.length === 0) return;
      setLines((previous) => {
        const next = [...previous, ...incoming];
        return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next;
      });
    };

    const onInit = (event: Event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { lines?: string[] };
        setLines((payload.lines ?? []).slice(-MAX_LINES));
        setState("live");
      } catch {
        setState("error");
      }
    };

    const onLine = (event: Event) => {
      try {
        const payload = JSON.parse((event as MessageEvent<string>).data) as { lines?: string[] };
        append(payload.lines ?? []);
        setState("live");
      } catch {
        /* skip an unparseable frame rather than killing the stream */
      }
    };

    const onEnd = () => {
      setState("closed");
      es.close();
      endRef.current();
    };

    es.addEventListener("init", onInit);
    es.addEventListener("line", onLine);
    es.addEventListener("end", onEnd);
    es.addEventListener("error", () => setState((prev) => (prev === "closed" ? prev : "error")));

    return () => {
      es.removeEventListener("init", onInit);
      es.removeEventListener("line", onLine);
      es.removeEventListener("end", onEnd);
      es.close();
    };
  }, []);

  useEffect(() => {
    if (!stickRef.current) return;
    const box = boxRef.current;
    if (box) box.scrollTop = box.scrollHeight;
  }, [lines]);

  const onScroll = useCallback(() => {
    const box = boxRef.current;
    if (!box) return;
    setStick(box.scrollHeight - box.scrollTop - box.clientHeight < 28);
  }, []);

  const statusCopy =
    state === "connecting"
      ? "connecting"
      : state === "error"
        ? "stream interrupted — retrying"
        : running
          ? "streaming"
          : "idle";

  return (
    <section className="card overflow-hidden" aria-label="Live run log">
      <header className="flex flex-wrap items-center gap-3 border-b border-zinc-800 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span
            aria-hidden
            className={[
              "h-1.5 w-1.5 rounded-full",
              state === "error"
                ? "bg-rose-400"
                : running
                  ? "live-dot bg-emerald-400"
                  : "bg-zinc-600",
            ].join(" ")}
          />
          <h2 className="text-sm font-medium text-zinc-200">Live run log</h2>
        </div>
        <span className="label-caps">{statusCopy}</span>
        <code className="hidden truncate font-mono text-[11px] text-zinc-600 sm:block">
          logs/realrun.log
        </code>

        <div className="ml-auto flex items-center gap-2">
          {!stick ? (
            <button
              type="button"
              onClick={() => {
                setStick(true);
                const box = boxRef.current;
                if (box) box.scrollTop = box.scrollHeight;
              }}
              className="btn-ghost h-7 px-2 py-0 text-xs"
            >
              Jump to latest
            </button>
          ) : null}
          <button type="button" onClick={onClose} className="btn-ghost h-7 px-2 py-0 text-xs">
            Hide
          </button>
        </div>
      </header>

      <div
        ref={boxRef}
        onScroll={onScroll}
        className="scroll-thin h-[320px] overflow-y-auto bg-zinc-950/60 px-4 py-3 font-mono text-[11.5px] leading-[1.65]"
      >
        {lines.length === 0 ? (
          <p className="flex items-center gap-2 text-zinc-500">
            {state === "connecting" ? <Spinner /> : null}
            {state === "error"
              ? "Could not reach the log stream."
              : "Waiting for output from the bot…"}
          </p>
        ) : (
          lines.map((line, index) => (
            <div
              key={`${index}-${line.slice(0, 24)}`}
              className={`break-words whitespace-pre-wrap ${lineTone(line)}`}
            >
              {line === "" ? "\u00a0" : line}
            </div>
          ))
        )}
      </div>
    </section>
  );
}
