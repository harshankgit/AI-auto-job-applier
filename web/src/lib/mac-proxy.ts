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
  const key = req.headers.get("x-jobbot-key") || src.searchParams.get("key") || "";
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

type Handler = (req: Request) => Response | Promise<Response>;

/**
 * Wrap a route handler: on the Mac (project root present) it runs unchanged;
 * on the hosted site it forwards keyed requests to the Mac and otherwise
 * answers from the snapshot, tagging every response with x-jobbot-link.
 */
export function withMac(handler: Handler): (req: Request) => Promise<Response> {
  return async (req: Request) => {
    if (rootIsPresent()) return handler(req);
    const { res, link } = await proxyToMac(req);
    if (res) return res;
    return tag(await handler(req), link);
  };
}
