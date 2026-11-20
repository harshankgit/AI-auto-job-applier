import { withMac } from "@/lib/mac-proxy";
import { getStatus, lastActivityAt, subscribe, tailLines } from "@/lib/runner";
import type { RunStatus } from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const BACKLOG = 200;
const HEARTBEAT_MS = 15_000;
const IDLE_GRACE_MS = 25_000;

function sse(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * `?tail=N` -> plain JSON snapshot (fallback for clients without EventSource).
 * Otherwise a Server-Sent Events stream of new log lines that closes itself
 * when the run finishes or the log goes quiet with nothing running.
 */
async function handleGET(request: Request) {
  const url = new URL(request.url);
  const tailParam = url.searchParams.get("tail");

  if (tailParam !== null) {
    const requested = Number.parseInt(tailParam, 10);
    const limit = Number.isFinite(requested) && requested > 0 ? Math.min(requested, 2000) : BACKLOG;
    const [lines, status] = await Promise.all([tailLines(limit), getStatus()]);
    return Response.json(
      { lines, running: status.running, status },
      { headers: { "cache-control": "no-store" } },
    );
  }

  const encoder = new TextEncoder();
  const backlog = await tailLines(BACKLOG);
  const initial = await getStatus();

  let unsubscribe: (() => void) | null = null;
  let heartbeat: ReturnType<typeof setInterval> | null = null;
  let idleCheck: ReturnType<typeof setInterval> | null = null;

  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      let closed = false;

      const send = (event: string, data: unknown) => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(sse(event, data)));
        } catch {
          closed = true;
        }
      };

      const shutdown = (reason: string, status: RunStatus | null) => {
        if (closed) return;
        send("end", { reason, status });
        closed = true;
        unsubscribe?.();
        unsubscribe = null;
        if (heartbeat) clearInterval(heartbeat);
        if (idleCheck) clearInterval(idleCheck);
        try {
          controller.close();
        } catch {
          /* already closed */
        }
      };

      send("init", { lines: backlog, status: initial });

      unsubscribe = subscribe(
        (lines) => send("line", { lines }),
        (status) => shutdown("run-finished", status),
      );

      heartbeat = setInterval(() => {
        if (closed) return;
        try {
          controller.enqueue(encoder.encode(": keep-alive\n\n"));
        } catch {
          shutdown("client-gone", null);
        }
      }, HEARTBEAT_MS);
      heartbeat.unref?.();

      // Guard against an orphaned stream: nothing running and no new output.
      idleCheck = setInterval(() => {
        if (closed) return;
        void getStatus().then((status) => {
          if (status.running) return;
          const quietFor = Date.now() - (lastActivityAt() || 0);
          if (quietFor > IDLE_GRACE_MS) shutdown("idle", status);
        });
      }, 5_000);
      idleCheck.unref?.();

      request.signal.addEventListener("abort", () => shutdown("client-gone", null));
    },
    cancel() {
      unsubscribe?.();
      if (heartbeat) clearInterval(heartbeat);
      if (idleCheck) clearInterval(idleCheck);
    },
  });

  return new Response(stream, {
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      "cache-control": "no-store, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}

export const GET = withMac(handleGET);
