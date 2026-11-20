import { NextResponse, type NextRequest } from "next/server";

/**
 * Two jobs, one file.
 *
 * 1. On Vercel: the hosted site cannot run Chrome or Python, so every /api/*
 *    call that carries the access key (x-jobbot-key header or ?key=) is
 *    forwarded to the Mac through its Cloudflare tunnel. The Mac publishes its
 *    current tunnel URL to an ntfy topic as "URL HMAC(key, URL)"; only a URL
 *    whose signature verifies with the caller's key is ever contacted, so a
 *    forged URL never receives the key. Without a key (or when the Mac is
 *    offline) GET requests fall back to the bundled snapshot.
 *
 * 2. On the Mac: requests that arrive through the tunnel must carry the same
 *    key, and only /api/* is reachable that way.
 */

const TOPIC = process.env.JOBBOT_NTFY_TOPIC || "jobbot-link-04021e998f9b1712";
const TUNNEL_RE = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/;
const CACHE_MS = 60_000;

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
    const text = await res.text();
    const messages = text
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
  cache.set(key, out);
  return out;
}

function withLink(res: NextResponse, link: string): NextResponse {
  res.headers.set("x-jobbot-link", link);
  return res;
}

function offline(req: NextRequest, link: string, message: string): NextResponse {
  if (req.method === "GET" || req.method === "HEAD") return withLink(NextResponse.next(), link);
  return withLink(NextResponse.json({ error: message }, { status: 502 }), link);
}

async function forward(req: NextRequest, base: string, key: string): Promise<Response> {
  const target = new URL(req.nextUrl.pathname + req.nextUrl.search, base);
  target.searchParams.delete("key");
  const headers = new Headers();
  req.headers.forEach((value, name) => {
    const n = name.toLowerCase();
    if (n === "host" || n === "connection" || n === "content-length" || n.startsWith("x-vercel") || n.startsWith("x-forwarded")) return;
    headers.set(name, value);
  });
  headers.set("x-jobbot-key", key);
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  const isStream = req.nextUrl.pathname === "/api/logs" && !req.nextUrl.searchParams.has("tail");
  const upstream = await fetch(target, {
    method: req.method,
    headers,
    body: hasBody ? await req.arrayBuffer() : undefined,
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

async function onVercel(req: NextRequest): Promise<Response> {
  const key = req.headers.get("x-jobbot-key") || req.nextUrl.searchParams.get("key") || "";
  if (!key) return withLink(NextResponse.next(), "nokey");

  let resolved = await resolveTunnel(key);
  if (!resolved.url) resolved = await resolveTunnel(key, true);
  if (!resolved.url) {
    return resolved.anyMessage
      ? offline(req, "badkey", "That access key is not valid for this Mac.")
      : offline(req, "offline", "The Mac is not reachable right now (it has to be on and online).");
  }
  try {
    const res = await forward(req, resolved.url, key);
    if (res.status === 401 || res.status === 403) {
      const r = withLink(NextResponse.json({ error: "Access key rejected by the Mac." }, { status: 401 }), "badkey");
      return r;
    }
    if (res.status >= 520 && res.status <= 530) throw new Error("tunnel down");
    return res;
  } catch {
    cache.delete(key);
    const retry = await resolveTunnel(key, true);
    if (retry.url && retry.url !== resolved.url) {
      try {
        return await forward(req, retry.url, key);
      } catch {
        /* fall through */
      }
    }
    return offline(req, "offline", "The Mac is not reachable right now (it has to be on and online).");
  }
}

function onMac(req: NextRequest): Response {
  const host = req.headers.get("host") || "";
  const viaTunnel = !!req.headers.get("cf-connecting-ip") || host.endsWith(".trycloudflare.com");
  if (!viaTunnel) return NextResponse.next();
  if (!req.nextUrl.pathname.startsWith("/api/")) {
    return new NextResponse("Not found", { status: 404 });
  }
  const expected = process.env.JOBBOT_ACCESS_KEY || "";
  const key = req.headers.get("x-jobbot-key") || req.nextUrl.searchParams.get("key") || "";
  if (!expected) return NextResponse.json({ error: "remote access is not configured on this Mac" }, { status: 403 });
  if (key !== expected) return NextResponse.json({ error: "bad access key" }, { status: 401 });
  return NextResponse.next();
}

export async function middleware(req: NextRequest): Promise<Response> {
  const isVercel = !!process.env.VERCEL || req.nextUrl.hostname.endsWith(".vercel.app");
  if (isVercel) {
    if (!req.nextUrl.pathname.startsWith("/api/")) return NextResponse.next();
    return onVercel(req);
  }
  return onMac(req);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
