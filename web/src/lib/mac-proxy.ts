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
          return JSON.parse(line) as { event?: string; message?: string };
        } catch {
          return null;
        }
      })
      .filter((m): m is { event: string; message: string } => !!m && m.event === "message" && !!m.message);
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

/* ------------------------------------------------------------ cloud runner */
// When the Mac is offline the GitHub Actions runner (scripts/cloud.py) applies
// instead. It publishes signed heartbeats to <topic>-cloud and obeys signed
// "start"/"stop" commands on <topic>-ctl.

type Heartbeat = {
  running: boolean;
  source: "cloud";
  candidate: string;
  startedAt: string;
  at: number;
  note?: string;
  run?: string;
  submittedCompanies?: Record<string, number>;
  tail?: string[];
};
const HEARTBEAT_FRESH_S = 15 * 60;

function accessKey(req: Request): string {
  return (
    req.headers.get("x-jobbot-key") ||
    new URL(req.url).searchParams.get("key") ||
    process.env.JOBBOT_ACCESS_KEY ||
    DEFAULT_KEY
  );
}

async function ntfyMessages(topic: string): Promise<string[]> {
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

/** Tell the cloud runner (and the Mac watchdog) to start or pause. */
export async function publishControl(cmd: "start" | "stop", key: string): Promise<boolean> {
  const ts = String(Math.floor(Date.now() / 1000));
  try {
    const res = await fetch(`https://ntfy.sh/${TOPIC}-ctl`, {
      method: "POST",
      body: `${cmd} ${ts} ${await hmacHex(key, `${cmd} ${ts}`)}`,
      signal: AbortSignal.timeout(8000),
    });
    return res.ok;
  } catch {
    return false;
  }
}

function json(data: unknown, link: string, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json", "x-jobbot-link": link, "cache-control": "no-store" },
  });
}

/** Answers status/logs/apply/stop from the cloud runner while the Mac is offline. */
async function cloudAnswer(req: Request, key: string): Promise<{ res: Response | null; link: string }> {
  const path = new URL(req.url).pathname;
  const [hb, ctl] = await Promise.all([lastHeartbeat(key), lastControl(key)]);
  const fresh = !!hb && Date.now() / 1000 - hb.at < HEARTBEAT_FRESH_S;
  const running = fresh && !!hb?.running && ctl.cmd !== "stop";
  const pending = !running && ctl.cmd === "start" && Date.now() / 1000 - ctl.at < 45 * 60;
  const link = running || pending ? "cloud" : "offline";
  const status = {
    running: running || pending,
    pid: null,
    startedAt: running ? hb!.startedAt : pending ? new Date(ctl.at * 1000).toISOString() : null,
    candidate: "both",
    source: running || pending ? "cloud" : null,
    lastRun: null,
    cloud: hb
      ? { at: hb.at, note: hb.note ?? "", run: hb.run ?? "", submittedCompanies: hb.submittedCompanies ?? {}, pending }
      : { pending },
  };
  if (req.method === "POST" && path === "/api/apply") {
    const ok = await publishControl("start", key);
    if (!ok) return { res: jsonError(502, "Could not reach the cloud runner queue (ntfy).", "offline"), link: "offline" };
    return {
      res: json(
        {
          started: true,
          candidate: "both",
          candidateLabel: "Both (cloud runner)",
          limit: 0,
          command: "GitHub Actions cloud runner (starts within ~30 min while the Mac is offline)",
          status: { ...status, running: true, source: "cloud", startedAt: status.startedAt ?? new Date().toISOString() },
        },
        "cloud",
      ),
      link: "cloud",
    };
  }
  if (req.method === "POST" && path === "/api/stop") {
    const ok = await publishControl("stop", key);
    if (!ok) return { res: jsonError(502, "Could not reach the cloud runner queue (ntfy).", "offline"), link: "offline" };
    return { res: json({ stopped: true, pids: [], status: { ...status, running: false, source: null } }, "offline"), link: "offline" };
  }
  if (req.method === "GET" && path === "/api/status") return { res: json(status, link), link };
  if (req.method === "GET" && path === "/api/logs" && new URL(req.url).searchParams.has("tail")) {
    const lines = hb?.tail?.length
      ? [`[cloud runner${hb.run ? " " + hb.run : ""} · heartbeat ${new Date(hb.at * 1000).toISOString()}]`, ...hb.tail]
      : ["[Mac offline — no cloud run yet]"];
    return { res: json({ lines, running: status.running, status }, link), link };
  }
  if (req.method === "GET" || req.method === "HEAD") return { res: null, link };
  return { res: jsonError(502, OFFLINE_MSG, "offline"), link: "offline" };
}

type Handler = (req: Request) => Response | Promise<Response>;

/**
 * Wrap a route handler: on the Mac (project root present) it runs unchanged;
 * on the hosted site it forwards keyed requests to the Mac and otherwise
 * answers from the snapshot, tagging every response with x-jobbot-link.
 */
export function withMac(handler: Handler): (req: Request) => Promise<Response> {
  return async (req: Request) => {
    const path = new URL(req.url).pathname;
    const control = req.method === "POST" && (path === "/api/apply" || path === "/api/stop");
    if (rootIsPresent()) {
      const res = await handler(req);
      // Mirror Start/Stop to the cloud runner so it pauses/resumes too.
      if (control && res.ok) await publishControl(path === "/api/stop" ? "stop" : "start", accessKey(req));
      return res;
    }
    const { res, link } = await proxyToMac(control ? req.clone() : req);
    if (res && link === "live") {
      if (control && res.ok) await publishControl(path === "/api/stop" ? "stop" : "start", accessKey(req));
      return res;
    }
    if (link === "offline") {
      const cloud = await cloudAnswer(req, accessKey(req));
      if (cloud.res) return cloud.res;
      return tag(await handler(req), cloud.link);
    }
    if (res) return res;
    return tag(await handler(req), link);
  };
}
