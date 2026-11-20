"""Self-test + self-heal loop for the whole jobbot setup.

Run by launchd every 15 minutes (com.jobbot.selftest). Each run:
  1. local dashboard (port 3001)         -> restart it if down
  2. bot process + log freshness         -> restart via start_bot.sh watchdog
  3. Cloudflare tunnel (Mac <-> internet) -> ask tunnel.sh to restart / republish
  4. Vercel site pages + live API link    -> republish tunnel URL if offline
  5. real browser test of the Vercel site (headless Chromium): badge must say
     Live, the dashboard must show the bot status, no 5xx console errors
  6. bot log scan for crashes / tracebacks
Results go to logs/selftest.log (history) and data/selftest.json (latest,
shown on the site through /api/health).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = os.environ.get("JOBBOT_SITE", "https://job-applier-chi.vercel.app")
LOCAL = "http://localhost:3001"
LOG = ROOT / "logs" / "selftest.log"
OUT = ROOT / "data" / "selftest.json"
ENV = os.environ.copy()
ENV["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + ENV.get("PATH", "")


def read_key() -> str:
    env = ROOT / "web" / ".env.local"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("JOBBOT_ACCESS_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


KEY = read_key()


def log(msg: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def http(url: str, key: bool = False, timeout: int = 25) -> tuple[int, dict[str, str], str]:
    req = urllib.request.Request(url, headers={"user-agent": "jobbot-selftest"})
    if key and KEY:
        req.add_header("x-jobbot-key", KEY)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read(200_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read(20_000).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - network errors of any kind are a failed check
        return 0, {}, str(exc)


def sh(*args: str, wait: bool = True) -> None:
    if wait:
        subprocess.run(list(args), cwd=ROOT, env=ENV, timeout=120, check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(list(args), cwd=ROOT, env=ENV, start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def running(pattern: str) -> bool:
    return subprocess.run(["pgrep", "-f", pattern], stdout=subprocess.DEVNULL).returncode == 0


class Run:
    def __init__(self) -> None:
        self.checks: list[dict[str, object]] = []
        self.fixes: list[str] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append({"name": name, "ok": ok, "detail": detail[:300]})
        log(f"{'PASS' if ok else 'FAIL'} {name} {detail[:200]}")
        return ok

    def fix(self, what: str) -> None:
        self.fixes.append(what)
        log(f"FIX  {what}")


def check_dashboard(r: Run) -> bool:
    bad = []
    body = ""
    for path in ("/api/health", "/api/status", "/api/applications", "/"):
        code, _, body = http(f"{LOCAL}{path}", timeout=90)
        if code != 200:
            bad.append(f"{path} HTTP {code}")
    if r.check("local dashboard", not bad, "; ".join(bad) or "all endpoints 200"):
        return True
    r.fix("restarting local dashboard (port 3001) with a clean .next cache")
    subprocess.run("pkill -f 'next dev --port 3001'; lsof -ti tcp:3001 | xargs kill", shell=True, check=False)
    time.sleep(3)
    shutil.rmtree(ROOT / "web" / ".next", ignore_errors=True)
    sh("bash", "-c", "cd web && nohup npx next dev --port 3001 > ../logs/dashboard.log 2>&1 &")
    for _ in range(40):
        time.sleep(3)
        if http(f"{LOCAL}/api/status", timeout=90)[0] == 200:
            return r.check("local dashboard after fix", True)
    return r.check("local dashboard after fix", False, body)


def check_bot(r: Run) -> None:
    alive = running(r"jobbot\.cli (apply|run)")
    log_file = ROOT / "logs" / "realrun.log"
    age = time.time() - log_file.stat().st_mtime if log_file.exists() else 1e9
    stopped = (ROOT / "data" / "STOP").exists()
    if alive and age < 1500:
        r.check("bot running", True, f"log updated {int(age)}s ago")
    elif stopped and not alive:
        r.check("bot running", True, "stopped by user (data/STOP) - left alone")
        return
    else:
        r.check("bot running", False, f"alive={alive} log age={int(age)}s")
        r.fix("start_bot.sh watchdog (restart / unfreeze bot)")
        sh("/bin/bash", str(ROOT / "scripts" / "start_bot.sh"), "watchdog")
        time.sleep(10)
        r.check("bot running after fix", running(r"jobbot\.cli (apply|run)"))
    if log_file.exists():
        tail = log_file.read_text(errors="replace").splitlines()[-400:]
        crashes = [ln for ln in tail if re.search(r"Traceback|CRITICAL|Fatal", ln)]
        r.check("bot log clean", len(crashes) < 3, f"{len(crashes)} tracebacks in last 400 lines")


def check_tunnel(r: Run) -> str:
    url_file = ROOT / "data" / "tunnel_url.txt"
    if not running(r"scripts/tunnel\.sh"):
        r.check("tunnel loop", False, "tunnel.sh not running")
        r.fix("starting scripts/tunnel.sh")
        sh("/bin/bash", "-c", "nohup /bin/bash scripts/tunnel.sh >/dev/null 2>&1 &")
        time.sleep(45)
    url = url_file.read_text().strip() if url_file.exists() else ""
    code = http(f"{url}/api/health", key=True)[0] if url else 0
    if r.check("tunnel reachable", code == 200, f"{url} HTTP {code}"):
        bad = http(f"{url}/api/status")[0]
        r.check("tunnel rejects requests without key", bad == 401, f"HTTP {bad}")
        return url
    r.fix("asking tunnel.sh to open a new tunnel")
    (ROOT / "data" / "tunnel_restart").touch()
    for _ in range(12):
        time.sleep(10)
        url = url_file.read_text().strip() if url_file.exists() else ""
        if url and http(f"{url}/api/health", key=True)[0] == 200:
            r.check("tunnel reachable after fix", True, url)
            return url
    r.check("tunnel reachable after fix", False)
    return ""


def check_vercel(r: Run) -> None:
    for page in ("/", "/applications", "/results", "/report"):
        code = http(f"{SITE}{page}")[0]
        r.check(f"vercel page {page}", code == 200, f"HTTP {code}")
    code, headers, body = http(f"{SITE}/api/health", key=True)
    link = headers.get("x-jobbot-link", "?")
    if r.check("vercel -> Mac live link", code == 200 and link == "live", f"HTTP {code} link={link}"):
        return
    r.fix("republishing tunnel URL to ntfy")
    (ROOT / "data" / "tunnel_republish").touch()
    time.sleep(75)  # tunnel loop wakes every 30s, Vercel caches the URL 60s
    code, headers, _ = http(f"{SITE}/api/health", key=True)
    r.check("vercel live link after fix", headers.get("x-jobbot-link") == "live", f"HTTP {code}")


def check_browser(r: Run) -> None:
    """Open the real site in headless Chromium the way a user would."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        r.check("browser test", False, "playwright not installed")
        return
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("response", lambda res: errors.append(f"{res.status} {res.url}")
                    if res.status >= 500 and "/api/" in res.url else None)
            page.goto(SITE, wait_until="domcontentloaded", timeout=60_000)
            page.evaluate("k => localStorage.setItem('jobbot-key', k)", KEY)
            page.reload(wait_until="networkidle", timeout=60_000)
            badge = page.get_by_role("button", name="Mac connection")
            badge.wait_for(timeout=30_000)
            page.wait_for_function(
                "() => !document.querySelector('[aria-label=\"Mac connection\"]').textContent.includes('Checking')",
                timeout=30_000)
            text = badge.inner_text()
            r.check("browser: badge says Live", "Live" in text, text)
            body = page.inner_text("main")
            bot_up = running(r"jobbot\.cli (apply|run)")
            expected = "running" if bot_up else "idle"
            r.check("browser: dashboard shows bot status", expected in body.lower(),
                    f"expected '{expected}' on page")
            for path in ("/applications", "/results", "/report"):
                page.goto(SITE + path, wait_until="networkidle", timeout=60_000)
            r.check("browser: no 5xx API errors", not errors, "; ".join(errors[:3]))
            browser.close()
    except Exception as exc:  # noqa: BLE001
        r.check("browser test", False, f"{type(exc).__name__}: {exc}")


def main() -> None:
    r = Run()
    started = time.time()
    log("---- self-test start ----")
    if check_dashboard(r):
        check_bot(r)
        if check_tunnel(r):
            check_vercel(r)
            check_browser(r)
    else:
        check_bot(r)
    failed = [c["name"] for c in r.checks if not c["ok"]]
    result = {
        "at": datetime.now(timezone.utc).isoformat(),
        "ok": not failed,
        "failed": failed,
        "fixes": r.fixes,
        "checks": r.checks,
        "seconds": round(time.time() - started, 1),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, indent=2))
    log(f"---- self-test end: {'ALL OK' if not failed else 'FAILED: ' + ', '.join(failed)} "
        f"(fixes: {len(r.fixes)}) ----")


if __name__ == "__main__":
    main()
