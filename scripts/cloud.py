"""GitHub runner glue: the bot applies from GitHub Actions, so the website works with the Mac off.

    python scripts/cloud.py gate           # exit 0 = start a run, 1 = another GitHub run is already active
    python scripts/cloud.py run            # pull state, apply (or wait while paused), heartbeat, push state
    python scripts/cloud.py paused         # exit 0 if the user pressed Stop on the site
    python scripts/cloud.py cloud_running  # exit 0 if a GitHub run is applying right now (Mac watchdog yields)
    python scripts/cloud.py inbox          # apply answers/profile details typed on the website
    python scripts/cloud.py ctl start|stop
    python scripts/cloud.py status

Coordination happens over ntfy.sh topics derived from JOBBOT_NTFY_TOPIC:
  <topic>        signed tunnel URL published by the Mac (scripts/tunnel.sh)
  <topic>-ctl    signed "start"/"stop" commands from the website
  <topic>-cloud  signed heartbeats from the GitHub runner (status + log tail)
  <topic>-inbox  signed answers / profile details saved on the website
Everything is HMAC-signed with the access key so nobody can forge commands.
The GitHub runner is primary; the Mac bot is only a backup and yields while a GitHub run is applying.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
LOGS = ROOT / "logs"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import state_sync  # noqa: E402

TUNNEL_RE = re.compile(r"^https://[a-z0-9-]+\.trycloudflare\.com$")
HEARTBEAT_S = 300
FRESH_S = 2 * HEARTBEAT_S + 180
STATE_PUSH_S = 1800
REPORT_S = 1800
PAUSE_POLL_S = 30
RUN_MINUTES = int(os.environ.get("JOBBOT_RUN_MINUTES", "300"))
BOT_CMD = [sys.executable, "-u", "-m", "jobbot.cli", "apply", "--candidate", "both", "--continuous",
           "--workers", "2", "--india-remote-only", "--min-score", "50", "--min-score-floor", "40",
           "--skip-ats", "greenhouse,lever", "--no-sheet"]
PROFILE_FIELDS = ("github", "linkedin", "portfolio", "current_ctc", "expected_ctc", "notice_period",
                  "work_authorized_countries")


def _topic() -> str:
    return os.environ.get("JOBBOT_NTFY_TOPIC") or state_sync._env_file("JOBBOT_NTFY_TOPIC") \
        or "jobbot-link-04021e998f9b1712"


def _key() -> str:
    return os.environ.get("JOBBOT_ACCESS_KEY") or state_sync._env_file("JOBBOT_ACCESS_KEY") \
        or "7a5d7e02ff468d4897bc2b2e5b28ccf3"


def _sig(text: str) -> str:
    return hmac.new(_key().encode(), text.encode(), hashlib.sha256).hexdigest()


def _raw(topic: str, since: str = "12h") -> list[dict]:
    """ntfy messages on a topic (dicts with id/time/message), newest first."""
    try:
        r = httpx.get(f"https://ntfy.sh/{topic}/json", params={"poll": "1", "since": since}, timeout=15)
        out = []
        for line in r.text.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("event") == "message" and m.get("message"):
                out.append(m)
        return out[::-1]
    except httpx.HTTPError:
        return []


def _messages(topic: str, since: str = "12h") -> list[str]:
    return [m["message"] for m in _raw(topic, since)]


def _publish(topic: str, text: str) -> bool:
    try:
        return httpx.post(f"https://ntfy.sh/{topic}", content=text.encode(), timeout=15).is_success
    except httpx.HTTPError:
        return False


def _signed_json(msg: str) -> dict | None:
    body, _, sig = msg.rpartition("\n")
    if body and hmac.compare_digest(sig.strip(), _sig(body)):
        try:
            return json.loads(body)
        except ValueError:
            return None
    return None


def mac_running() -> bool:
    """True if the Mac's bot is running (reached through its signed tunnel)."""
    for m in _raw(_topic(), "1h"):
        parts = m["message"].split()
        if len(parts) != 2 or not TUNNEL_RE.match(parts[0]):
            continue
        if not hmac.compare_digest(parts[1], _sig(parts[0])):
            continue
        try:
            r = httpx.get(parts[0] + "/api/status", headers={"x-jobbot-key": _key()}, timeout=20)
            if r.status_code == 200:
                s = r.json()
                return bool(s.get("running")) and s.get("source") != "cloud"
        except (httpx.HTTPError, ValueError):
            pass
        return False  # newest valid URL is dead -> Mac offline
    return False


def control() -> tuple[str, float]:
    """Latest signed command from the website: ("start"|"stop"|"", unix time)."""
    for msg in _messages(f"{_topic()}-ctl"):
        parts = msg.split()
        if len(parts) == 3 and parts[0] in ("start", "stop") and \
                hmac.compare_digest(parts[2], _sig(f"{parts[0]} {parts[1]}")):
            return parts[0], float(parts[1])
    return "", 0.0


def publish_control(cmd: str) -> bool:
    ts = str(int(time.time()))
    return _publish(f"{_topic()}-ctl", f"{cmd} {ts} {_sig(f'{cmd} {ts}')}")


def paused() -> bool:
    cmd, at = control()
    if cmd == "stop" and time.time() - at > 6 * 3600:
        publish_control("stop")  # ntfy keeps messages ~12h; refresh so the pause sticks
    return cmd == "stop"


def last_heartbeat() -> dict | None:
    for msg in _messages(f"{_topic()}-cloud"):
        hb = _signed_json(msg)
        if hb is not None:
            return hb
    return None


def cloud_running() -> bool:
    hb = last_heartbeat()
    return bool(hb and hb.get("running") and time.time() - hb.get("at", 0) < FRESH_S)


# --- inbox: answers / profile details typed on the hosted website -------------------------

def apply_inbox() -> int:
    """Apply signed website edits (oldest first) that this machine has not applied yet."""
    seen_path = DATA / "inbox_applied.json"
    try:
        seen = set(json.loads(seen_path.read_text()))
    except (OSError, ValueError):
        seen = set()
    applied = 0
    for m in reversed(_raw(f"{_topic()}-inbox")):
        if m.get("id") in seen:
            continue
        item = _signed_json(m["message"])
        seen.add(m.get("id"))
        if not item:
            continue
        try:
            if item.get("kind") == "learn":
                applied += _apply_learn(item)
            elif item.get("kind") == "profile":
                applied += _apply_profile(item)
        except Exception as exc:  # never let a bad message stop applying
            print(f"inbox: could not apply {m.get('id')}: {exc}")
    DATA.mkdir(exist_ok=True)
    seen_path.write_text(json.dumps(sorted(s for s in seen if s)[-500:]))
    if applied:
        print(f"inbox: applied {applied} website edit(s)")
    return applied


def _apply_learn(item: dict) -> int:
    sys.path.insert(0, str(ROOT))
    from jobbot import learn
    cid = str(item.get("candidate") or "")
    if cid not in ("harshank", "divya", "both"):
        return 0
    n = 0
    for it in item.get("items") or []:
        q, a = str(it.get("question") or "").strip(), str(it.get("answer") or "").strip()
        if not q:
            continue
        if a:
            learn.teach(cid, q[:500], a[:2000])
        else:  # empty answer = forget
            path = learn.LEARNED_PATH
            data = json.loads(path.read_text()) if path.exists() else {}
            data.get(cid, {}).pop(learn.norm(q), None)
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
        n += 1
    return n


def _apply_profile(item: dict) -> int:
    cid = str(item.get("candidate") or "")
    if cid not in ("harshank", "divya"):
        return 0
    path = DATA / "private_profile.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    row = data.setdefault(cid, {})
    for k, v in (item.get("details") or {}).items():
        if k not in PROFILE_FIELDS:
            continue
        v = str(v or "").strip()[:300]
        if not v:
            row.pop(k, None)
        elif k == "work_authorized_countries":
            row[k] = [c.strip() for c in v.split(",") if c.strip()]
        else:
            row[k] = v
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    return 1


# --- heartbeat + reports ----------------------------------------------------------------

def _counts() -> dict:
    import sqlite3
    db = DATA / "applications.db"
    if not db.exists():
        return {}
    with sqlite3.connect(db, timeout=30) as conn:
        rows = conn.execute(
            "SELECT candidate_id, COUNT(DISTINCT lower(trim(company))) FROM attempts "
            "WHERE status='SUBMITTED' GROUP BY candidate_id").fetchall()
    return {cid: n for cid, n in rows}


def _tail(path: Path, n: int = 40) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text(errors="replace").splitlines()[-n:]
    out, size = [], 0
    for ln in reversed(lines):
        ln = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", ln)
        ln = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", ln)[:220]
        size += len(ln) + 1
        if size > 2600:
            break
        out.append(ln)
    return out[::-1]


def heartbeat(running: bool, started: float, log_path: Path, note: str = "", state: str = "") -> None:
    body = json.dumps({
        "running": running, "source": "cloud", "candidate": "both",
        "state": state or ("applying" if running else "idle"),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "at": int(time.time()), "note": note,
        "run": os.environ.get("JOBBOT_RUN_URL", ""),
        "submittedCompanies": _counts(), "tail": _tail(log_path),
    }, separators=(",", ":"))
    _publish(f"{_topic()}-cloud", f"{body}\n{_sig(body)}")


def publish_reports() -> None:
    """Commit fresh reports to the repo so the hosted site (Vercel) redeploys with new data."""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    py = sys.executable
    try:
        subprocess.run([py, "-m", "jobbot.cli", "report", "--no-sheet"], cwd=ROOT, timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        subprocess.run([py, "-c", "from jobbot import learn; learn.write_reports()"], cwd=ROOT, timeout=300,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
        subprocess.run(["git", "add", "reports/"], cwd=ROOT, check=True)
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0:
            return
        subprocess.run(["git", "commit", "-q", "-m", "Update application reports (GitHub runner)"], cwd=ROOT,
                       check=True)
        for _ in range(3):
            subprocess.run(["git", "pull", "-q", "--rebase", "-X", "theirs", "origin", "main"], cwd=ROOT)
            if subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=ROOT).returncode == 0:
                print("run: reports published")
                return
            time.sleep(10)
    except Exception as exc:
        print(f"run: report publish failed: {exc}")


# --- gate + run --------------------------------------------------------------------------

def _run_finished(hb: dict) -> bool:
    """True when the heartbeat's GitHub run has already ended (cancelled/crashed runs leave a
    stale 'running' heartbeat behind for up to FRESH_S)."""
    m = re.search(r"/actions/runs/(\d+)", str(hb.get("run") or ""))
    if not m or m.group(1) == os.environ.get("GITHUB_RUN_ID"):
        return False
    repo = os.environ.get("JOBBOT_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return False
    try:
        out = subprocess.run(
            ["gh", "run", "view", m.group(1), "-R", repo, "--json", "status", "-q", ".status"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception:
        return False
    return out.returncode == 0 and out.stdout.strip() == "completed"


def gate() -> int:
    if cloud_running():
        hb = last_heartbeat() or {}
        if _run_finished(hb):
            print(f"gate: last heartbeat came from {hb.get('run')} which has finished -> run")
            return 0
        print("gate: another GitHub run is active -> skip")
        return 1
    print("gate: no GitHub run active -> run (it waits if Stop was pressed)")
    return 0


def _stop_bot(bot: subprocess.Popen) -> None:
    (DATA / "STOP").touch()
    try:
        bot.wait(600)
    except subprocess.TimeoutExpired:
        print("run: bot did not stop in 10 min -> terminate")
        bot.send_signal(signal.SIGTERM)
        try:
            bot.wait(60)
        except subprocess.TimeoutExpired:
            bot.kill()


def _push_state() -> None:
    try:
        state_sync.push()
    except Exception as exc:  # keep applying even if a sync fails
        print(f"run: state push failed: {exc}")


def run() -> int:
    LOGS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    os.environ.setdefault("JOBBOT_SITE", "cloud")
    state_sync.pull()
    apply_inbox()
    log_path = LOGS / "cloud-run.log"
    started = time.time()
    deadline = started + RUN_MINUTES * 60
    print(f"run: up to {RUN_MINUTES} min (bot output -> {log_path.name}, kept private)")
    last_push = last_report = time.time()
    reason = "time limit"
    while time.time() < deadline:
        # --- paused: wait for Start on the website ---
        if paused():
            print("run: paused from the website -> waiting for Start")
            last_hb = 0.0
            while paused() and time.time() < deadline:
                if time.time() - last_hb > HEARTBEAT_S:
                    last_hb = time.time()
                    heartbeat(False, started, log_path, "paused - press Start on the site", "paused")
                time.sleep(PAUSE_POLL_S)
            continue
        # --- applying ---
        for p in DATA.glob("STOP*"):
            p.unlink()
        with open(log_path, "a") as fh:
            bot = subprocess.Popen(BOT_CMD, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
        print(f"run: bot started (pid {bot.pid})")
        heartbeat(True, started, log_path, "started")
        last_hb = last_check = time.time()
        while bot.poll() is None:
            time.sleep(15)
            now = time.time()
            if now - last_check > 45:
                last_check = now
                why = "time limit" if now > deadline else "Stop pressed on site" if paused() else ""
                if why:
                    print(f"run: stopping bot ({why})")
                    heartbeat(False, started, log_path, why, "stopping")
                    _stop_bot(bot)
                    break
            if now - last_hb > HEARTBEAT_S:
                last_hb = now
                apply_inbox()
                heartbeat(True, started, log_path)
            if now - last_push > STATE_PUSH_S:
                last_push = now
                _push_state()
            if now - last_report > REPORT_S:
                last_report = now
                publish_reports()
        print(f"run: bot exited with code {bot.returncode}; submitted companies {_counts()}")
        if time.time() < deadline and not paused():
            heartbeat(True, started, log_path, "bot restarting")
            time.sleep(60)
    for _ in range(3):
        try:
            state_sync.push()
            break
        except Exception as exc:
            print(f"run: final state push failed ({exc}), retry")
            time.sleep(20)
    # the next scheduled/queued run takes over; say "starting" so the site does not flash "idle"
    heartbeat(False, started, log_path, reason, "paused" if paused() else "handover")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "gate":
        return gate()
    if cmd == "run":
        return run()
    if cmd == "paused":
        return 0 if paused() else 1
    if cmd == "cloud_running":
        return 0 if cloud_running() else 1
    if cmd == "inbox":
        apply_inbox()
        return 0
    if cmd == "ctl" and len(sys.argv) > 2 and sys.argv[2] in ("start", "stop"):
        return 0 if publish_control(sys.argv[2]) else 1
    if cmd == "status":
        print(json.dumps({"mac_running": mac_running(), "cloud_running": cloud_running(),
                          "control": control(), "heartbeat": last_heartbeat()}, indent=1)[:3000])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
