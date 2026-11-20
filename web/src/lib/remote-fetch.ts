"use client";

/**
 * Adds the saved access key to every same-origin /api request. Patched at
 * module load (not in an effect) so it is in place before any component's
 * first fetch. The key lives only in this browser's localStorage.
 */
export const KEY_STORAGE = "jobbot-key";

export function getKey(): string {
  try {
    return window.localStorage.getItem(KEY_STORAGE) || "";
  } catch {
    return "";
  }
}

export function isRemoteHost(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return !(h === "localhost" || h === "127.0.0.1" || h === "::1" || h.endsWith(".local"));
}

function isApi(url: string): boolean {
  try {
    const u = new URL(url, window.location.href);
    return u.origin === window.location.origin && u.pathname.startsWith("/api/");
  } catch {
    return false;
  }
}

declare global {
  interface Window {
    __jobbotFetchPatched?: boolean;
  }
}

if (typeof window !== "undefined" && !window.__jobbotFetchPatched) {
  window.__jobbotFetchPatched = true;
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const key = getKey();
    if (key && isApi(url)) {
      const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
      headers.set("x-jobbot-key", key);
      init = { ...init, headers };
    }
    return original(input, init);
  };
}
