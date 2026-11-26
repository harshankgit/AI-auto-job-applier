import { rootIsPresent } from "@/lib/paths";

/**
 * Hosted (Vercel) side of the Mac link.
 *
 * The hosted site cannot run Chrome or Python, so when the project root is not
 * present every /api/* call that carries the access key (x-jobbot-key header or
 * ?key=) is forwarded to the Mac through its Cloudflare tunnel. The Mac
 * publishes its current tunnel URL to an ntfy topic as "URL HMAC(key, URL)";
 * only a URL whose signature verifies with the caller's key is contacted, so a
 * forged URL never receives the key. Without a key, or when the Mac is
 * offline, GET requests fall back to the bundled snapshot.
 *
 * This runs inside the route handlers (not middleware) because the legacy
 * vercel.json build used for this project does not execute Next middleware.
 */

const TOPIC = process.env.JOBBOT_NTFY_TOPIC || "jobbot-link-04021e998f9b1712";
const TUNNEL_RE = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/;
const CACHE_MS = 60_000;
const OFFLINE_MSG = "The Mac is not reachable right now (it has to be on and online).";

type Resolved = { url: string | null; anyMessage: boolean; at: number };
const cache = new Map<string, Resolved>();

const DEFAULT_KEY = "7a5d7e02ff468d4897bc2b2e5b28ccf3";

async function hmacHex(key: string, data: string): Promise<string> {
  const enc = new TextEncoder();
  const k = await crypto.subtle.importKey("raw", enc.encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = new Uint8Array(await crypto.subtle.sign("HMAC", k, enc.encode(data)));
  return Array.from(sig, (b) => b.toString(16).padStart(2, "0")).join("");
}

async function resolveTunnel(key: string, fresh = false): Promise<Resolved> {
  const hit = cache.get(key);
  if (!fresh && hit && Date.now() - hit.at < CACHE_MS) return hit;
  let url: string | null = null;
  let anyMessage = false;
  try {
    const res = await fetch(`https://ntfy.sh/${TOPIC}/json?poll=1&since=12h`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const messages = (await res.text())
      .split("\n")
      .map((line) => {
        try {
          return JSON.parse(line) as { event?: string; message?: string; time?: number };
        } catch {
          return null;
        }
      })
      .filter(
        (m): m is { event: string; message: string; time?: number } =>
          !!m && m.event === "message" && !!m.message && (!m.time || Date.now() / 1000 - m.time < 25 * 60),
      );
    anyMessage = messages.length > 0;
    for (const m of messages.reverse()) {
      const [candidate, sig] = m.message.trim().split(/\s+/);
      if (!candidate || !sig || !TUNNEL_RE.test(candidate)) continue;
      if ((await hmacHex(key, candidate)) === sig.toLowerCase()) {
        url = candidate;
        break;
      }
    }
  } catch {
    /* ntfy unreachable: treat as offline */
  }
  const out = { url, anyMessage, at: Date.now() };
  if (url) cache.set(key, out);
  else cache.delete(key);
  return out;
}

function tag(res: Response, link: string): Response {
  try {
    res.headers.set("x-jobbot-link", link);
    return res;
  } catch {
    const headers = new Headers(res.headers);
    headers.set("x-jobbot-link", link);
    return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
  }
}

function jsonError(status: number, message: string, link: string): Response {
  return new Response(JSON.stringify({ error: message }), {
    status,
    headers: { "content-type": "application/json", "x-jobbot-link": link, "cache-control": "no-store" },
  });
}

async function forward(req: Request, body: ArrayBuffer | undefined, base: string, key: string): Promise<Response> {
  const src = new URL(req.url);
  const target = new URL(src.pathname + src.search, base);
  target.searchParams.delete("key");
  const headers = new Headers();
  req.headers.forEach((value, name) => {
    const n = name.toLowerCase();
    if (
      n === "host" ||
      n === "connection" ||
      n === "content-length" ||
      n === "accept-encoding" ||
      n.startsWith("x-vercel") ||
      n.startsWith("x-forwarded") ||
      n.startsWith("x-real-ip")
    )
      return;
    headers.set(name, value);
  });
  headers.set("x-jobbot-key", key);
  const isStream = src.pathname === "/api/logs" && !src.searchParams.has("tail");
  const upstream = await fetch(target, {
    method: req.method,
    headers,
    body,
    redirect: "manual",
    cache: "no-store",
    signal: isStream ? undefined : AbortSignal.timeout(25_000),
  });
  const out = new Headers(upstream.headers);
  out.delete("content-encoding");
  out.delete("content-length");
  out.delete("transfer-encoding");
  out.set("x-jobbot-link", "live");
  out.set("cache-control", "no-store");
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

/** Returns a proxied response, or null when the local handler should answer (with `link` to tag it). */
async function proxyToMac(req: Request): Promise<{ res: Response | null; link: string }> {
  const src = new URL(req.url);
  // Built-in default so the hosted site connects without the user typing anything.
  const key =
    req.headers.get("x-jobbot-key") || src.searchParams.get("key") || process.env.JOBBOT_ACCESS_KEY || DEFAULT_KEY;
  if (!key) return { res: null, link: "nokey" };
  const isRead = req.method === "GET" || req.method === "HEAD";
  const body = isRead ? undefined : await req.arrayBuffer();

  let resolved = await resolveTunnel(key);
  if (!resolved.url) resolved = await resolveTunnel(key, true);
  if (!resolved.url) {
    const link = resolved.anyMessage ? "badkey" : "offline";
    if (isRead) return { res: null, link };
    return {
      res: jsonError(
        link === "badkey" ? 401 : 502,
        link === "badkey" ? "That access key is not valid for this Mac." : OFFLINE_MSG,
        link,
      ),
      link,
    };
  }
  const attempt = async (base: string) => {
    const res = await forward(req, body, base, key);
    if (res.status === 401 || res.status === 403) return jsonError(401, "Access key rejected by the Mac.", "badkey");
    if (res.status >= 520 && res.status <= 530) throw new Error("tunnel down");
    return res;
  };
  try {
    return { res: await attempt(resolved.url), link: "live" };
  } catch {
    cache.delete(key);
    const retry = await resolveTunnel(key, true);
    if (retry.url && retry.url !== resolved.url) {
      try {
        return { res: await attempt(retry.url), link: "live" };
      } catch {
        /* fall through */
      }
    }
  }
  if (isRead) return { res: null, link: "offline" };
  return { res: jsonError(502, OFFLINE_MSG, "offline"), link: "offline" };
}

/* ------------------------------------------------------------ GitHub runner */
// The GitHub Actions runner (scripts/cloud.py) is the primary applier: it runs
// with the Mac off. It publishes signed heartbeats to <topic>-cloud, obeys
// signed "start"/"stop" commands on <topic>-ctl and reads saved answers /
// profile details from <topic>-inbox. The Mac is only a backup.

type Heartbeat = {
  running: boolean;
  source: "cloud";
  candidate: string;
  startedAt: string;
  at: number;
  note?: string;
  state?: string;
  run?: string;
  submittedCompanies?: Record<string, number>;
  tail?: string[];
};
const HEARTBEAT_FRESH_S = 15 * 60;
const PENDING_S = 35 * 60;

function accessKey(req: Request): string {
  return (
    req.headers.get("x-jobbot-key") ||
    new URL(req.url).searchParams.get("key") ||
    process.env.JOBBOT_ACCESS_KEY ||
    DEFAULT_KEY
  );
}

const NTFY_CACHE = new Map<string, { at: number; msgs: string[] }>();

async function ntfyMessages(topic: string): Promise<string[]> {
  const hit = NTFY_CACHE.get(topic);
  if (hit && Date.now() - hit.at < 15_000) return hit.msgs;
  const msgs = await ntfyFetch(topic);
  NTFY_CACHE.set(topic, { at: Date.now(), msgs });
  return msgs;
}

async function ntfyFetch(topic: string): Promise<string[]> {
  try {
    const res = await fetch(`https://ntfy.sh/${topic}/json?poll=1&since=12h`, {
      cache: "no-store",
      signal: AbortSignal.timeout(8000),
    });
    const out: string[] = [];
    for (const line of (await res.text()).split("\n")) {
      try {
        const m = JSON.parse(line) as { event?: string; message?: string };
        if (m.event === "message" && m.message) out.push(m.message);
      } catch {
        /* skip */
      }
    }
    return out.reverse();
  } catch {
    return [];
  }
}

async function lastHeartbeat(key: string): Promise<Heartbeat | null> {
  for (const msg of await ntfyMessages(`${TOPIC}-cloud`)) {
    const cut = msg.lastIndexOf("\n");
    if (cut < 0) continue;
    const body = msg.slice(0, cut);
    if ((await hmacHex(key, body)) !== msg.slice(cut + 1).trim()) continue;
    try {
      return JSON.parse(body) as Heartbeat;
    } catch {
      /* skip */
    }
  }
  return null;
}

async function lastControl(key: string): Promise<{ cmd: string; at: number }> {
  for (const msg of await ntfyMessages(`${TOPIC}-ctl`)) {
    const [cmd, ts, sig] = msg.trim().split(/\s+/);
    if ((cmd === "start" || cmd === "stop") && ts && sig && (await hmacHex(key, `${cmd} ${ts}`)) === sig) {
      return { cmd, at: Number(ts) };
    }
  }
  return { cmd: "", at: 0 };
}

async function ntfyPost(topic: string, body: string): Promise<boolean> {
  NTFY_CACHE.delete(topic);
  try {
    const res = await fetch(`https://ntfy.sh/${topic}`, { method: "POST", body, signal: AbortSignal.timeout(8000) });
    return res.ok;
  } catch {
    return false;
  }
}

/** Tell the GitHub runner (and the Mac watchdog) to start or pause. */
export async function publishControl(cmd: "start" | "stop", key: string): Promise<boolean> {
  const ts = String(Math.floor(Date.now() / 1000));
  return ntfyPost(`${TOPIC}-ctl`, `${cmd} ${ts} ${await hmacHex(key, `${cmd} ${ts}`)}`);
}

/** Queue a signed item (saved answers / profile details) for the GitHub runner. */
export async function publishInbox(item: Record<string, unknown>, key?: string): Promise<boolean> {
  const k = key || process.env.JOBBOT_ACCESS_KEY || DEFAULT_KEY;
  const body = JSON.stringify(item);
  return ntfyPost(`${TOPIC}-inbox`, `${body}\n${await hmacHex(k, body)}`);
}

function json(data: unknown, link: string, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "x-jobbot-link": link, "cache-control": "no-store" },
  });
}

type CloudState = { hb: Heartbeat | null; link: string; running: boolean; status: Record<string, unknown> };

/** Current state of the GitHub runner, as a dashboard RunStatus plus a link label. */
async function cloudState(key: string): Promise<CloudState> {
  const [hb, ctl] = await Promise.all([lastHeartbeat(key), lastControl(key)]);
  const now = Date.now() / 1000;
  const fresh = !!hb && now - hb.at < HEARTBEAT_FRESH_S;
  const stopped = ctl.cmd === "stop";
  const applying = fresh && !!hb?.running && !stopped;
  const pending = !applying && ctl.cmd === "start" && now - ctl.at < PENDING_S;
  const running = applying || pending;
  const link = running
    ? "cloud"
    : stopped || (fresh && hb?.state === "paused")
      ? "cloud-paused"
      : fresh
        ? "cloud-idle"
        : "cloud-down";
  const status = {
    running,
    pid: null,
    startedAt: applying ? hb!.startedAt : pending ? new Date(ctl.at * 1000).toISOString() : null,
    candidate: "both",
    source: running ? "cloud" : null,
    lastRun: null,
    cloud: hb
      ? {
          at: hb.at,
          note: hb.note ?? "",
          state: hb.state ?? "",
          run: hb.run ?? "",
          submittedCompanies: hb.submittedCompanies ?? {},
          pending,
          paused: stopped,
        }
      : { pending, paused: stopped },
  };
  return { hb, link, running, status };
}

function cloudLogs(c: CloudState): string[] {
  const hb = c.hb;
  if (hb?.tail?.length)
    return [`[GitHub runner${hb.run ? " " + hb.run : ""} · heartbeat ${new Date(hb.at * 1000).toISOString()}]`, ...hb.tail];
  if (hb)
    return [`[GitHub runner ${hb.note ?? ""} · ${new Date(hb.at * 1000).toISOString()} — log lines arrive with the next heartbeat (~5 min)]`];
  return ["[GitHub runner has not reported yet — it starts on the next schedule (every 30 min)]"];
}

type Handler = (req: Request) => Response | Promise<Response>;

/**
 * Wrap a route handler.
 *
 * On the Mac (project root present) it runs unchanged and mirrors Start/Stop
 * to the GitHub runner. On the hosted site the GitHub runner comes first:
 * status, logs and Start/Stop are answered from it, so the site works with
 * the Mac switched off. The Mac is only consulted as a backup (when its bot
 * is the one applying) and for fresher data reads.
 */
export function withMac(handler: Handler): (req: Request) => Promise<Response> {
  return async (req: Request) => {
    const url = new URL(req.url);
    const path = url.pathname;
    const key = accessKey(req);
    const isApply = req.method === "POST" && path === "/api/apply";
    const isStop = req.method === "POST" && path === "/api/stop";

    if (rootIsPresent()) {
      const res = await handler(req);
      if ((isApply || isStop) && res.ok) await publishControl(isStop ? "stop" : "start", key);
      if (req.method === "GET" && (path === "/api/status" || (path === "/api/logs" && url.searchParams.has("tail")))) {
        try {
          const body = (await res.clone().json()) as { running?: boolean; status?: { running?: boolean } };
          const macRunning = path === "/api/status" ? !!body.running : !!body.status?.running;
          if (!macRunning) {
            const c = await cloudState(key);
            if (c.running)
              return path === "/api/status"
                ? json(c.status, c.link)
                : json({ lines: cloudLogs(c), running: true, status: c.status }, c.link);
          }
        } catch {
          /* keep the Mac answer */
        }
      }
      return res;
    }

    // ---- hosted site: GitHub runner first ----
    if (isApply || isStop) {
      const ok = await publishControl(isStop ? "stop" : "start", key);
      if (isStop) await proxyToMac(req).catch(() => null); // best effort: stop a Mac backup run too
      if (!ok) return jsonError(502, "Could not reach the GitHub runner queue (ntfy). Try again.", "cloud-down");
      const c = await cloudState(key);
      if (isStop)
        return json({ stopped: true, pids: [], status: { ...c.status, running: false, source: null } }, "cloud-paused");
      return json(
        {
          started: true,
          candidate: "both",
          candidateLabel: "Both (GitHub runner)",
          limit: 0,
          command: "GitHub Actions runner — resumes within a minute if a run is active, otherwise on the next 30-min schedule",
          status: { ...c.status, running: true, source: "cloud", startedAt: c.status.startedAt ?? new Date().toISOString() },
        },
        "cloud",
      );
    }

    // Saved answers / profile details are queued for the runner by the handler itself.
    if (path === "/api/learn" || path === "/api/profile-details") return tag(await handler(req), "cloud");

    const isStatus = req.method === "GET" && path === "/api/status";
    const isTail = req.method === "GET" && path === "/api/logs" && url.searchParams.has("tail");
    const isHealth = req.method === "GET" && path === "/api/health";
    if (isStatus || isTail || isHealth) {
      const c = await cloudState(key);
      const cloudRes = () =>
        isStatus
          ? json(c.status, c.link)
          : isTail
            ? json({ lines: cloudLogs(c), running: c.running, status: c.status }, c.link)
            : json({ ok: true, root: false, running: c.running, cloud: c.status.cloud, at: new Date().toISOString() }, c.link);
      if (c.running) return cloudRes();
      // Runner idle: if the Mac backup is the one applying, show it.
      const { res, link } = await proxyToMac(req);
      if (res && link === "live" && res.ok) {
        try {
          const body = (await res.clone().json()) as { running?: boolean; status?: { running?: boolean } };
          if (isStatus ? body.running : isTail ? body.status?.running : body.running) return res;
        } catch {
          /* fall back to the runner */
        }
      }
      return cloudRes();
    }

    // Other reads: fresher data from the Mac when it is reachable, else the committed snapshot.
    const { res, link } = await proxyToMac(req);
    if (res && link === "live") return res;
    if (res && req.method !== "GET" && req.method !== "HEAD") return res;
    return tag(await handler(req), "cloud");
  };
}
