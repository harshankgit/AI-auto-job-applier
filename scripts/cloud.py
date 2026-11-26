"""Cloud runner glue: lets the bot keep applying from GitHub Actions while the Mac is off.

    python scripts/cloud.py gate          # exit 0 = cloud should run, 1 = skip (Mac busy / paused / cloud busy)
    python scripts/cloud.py run           # pull state, run the bot, heartbeat, push state
    python scripts/cloud.py paused        # exit 0 if the user pressed Stop on the site (Mac watchdog uses this)
    python scripts/cloud.py ctl start|stop

Coordination happens over three ntfy.sh topics derived from JOBBOT_NTFY_TOPIC:
  <topic>       signed tunnel URL published by the Mac (scripts/tunnel.sh)
  <topic>-ctl   signed "start"/"stop" commands from the website
  <topic>-cloud signed heartbeats from the cloud runner (status + log tail)
Everything is HMAC-signed with the access key so nobody can forge commands.
The Mac always wins: if its bot is running the cloud run stops itself.
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
HEARTBEAT_S = 360
STATE_PUSH_S = 1800
RUN_MINUTES = int(os.environ.get("JOBBOT_RUN_MINUTES", "300"))
BOT_CMD = [sys.executable, "-u", "-m", "jobbot.cli", "apply", "--candidate", "both", "--continuous",
           "--workers", "2", "--india-remote-only", "--min-score", "50", "--min-score-floor", "40",
           "--skip-ats", "greenhouse", "--no-sheet"]


def _topic() -> str:
    return os.environ.get("JOBBOT_NTFY_TOPIC") or state_sync._env_file("JOBBOT_NTFY_TOPIC") \
        or "jobbot-link-04021e998f9b1712"


def _key() -> str:
    return os.environ.get("JOBBOT_ACCESS_KEY") or state_sync._env_file("JOBBOT_ACCESS_KEY") \
        or "7a5d7e02ff468d4897bc2b2e5b28ccf3"


def _sig(text: str) -> str:
    return hmac.new(_key().encode(), text.encode(), hashlib.sha256).hexdigest()


def _messages(topic: str, since: str = "12h") -> list[str]:
    """Messages on a topic, newest first."""
    try:
        r = httpx.get(f"https://ntfy.sh/{topic}/json", params={"poll": "1", "since": since}, timeout=15)
        out = []
        for line in r.text.splitlines():
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if m.get("event") == "message" and m.get("message"):
                out.append(m["message"])
        return out[::-1]
    except httpx.HTTPError:
        return []


def _publish(topic: str, text: str) -> bool:
    try:
        return httpx.post(f"https://ntfy.sh/{topic}", content=text.encode(), timeout=15).is_success
    except httpx.HTTPError:
        return False


def mac_running() -> bool:
    """True if the Mac's bot is running (reached through its signed tunnel)."""
    for msg in _messages(_topic()):
        parts = msg.split()
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
        body, _, sig = msg.rpartition("\n")
        if body and hmac.compare_digest(sig.strip(), _sig(body)):
            try:
                return json.loads(body)
            except ValueError:
                continue
    return None


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
        ln = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "<email>", ln)[:220]
        size += len(ln) + 1
        if size > 2600:
            break
        out.append(ln)
    return out[::-1]


def heartbeat(running: bool, started: float, log_path: Path, note: str = "") -> None:
    body = json.dumps({
        "running": running, "source": "cloud", "candidate": "both",
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "at": int(time.time()), "note": note,
        "run": os.environ.get("JOBBOT_RUN_URL", ""),
        "submittedCompanies": _counts(), "tail": _tail(log_path),
    }, separators=(",", ":"))
    _publish(f"{_topic()}-cloud", f"{body}\n{_sig(body)}")


def gate() -> int:
    if paused():
        print("gate: paused from the website (Stop) -> skip")
        return 1
    if mac_running():
        print("gate: Mac bot is running -> skip")
        return 1
    hb = last_heartbeat()
    if hb and hb.get("running") and time.time() - hb.get("at", 0) < 2 * HEARTBEAT_S + 120:
        print("gate: another cloud run is active -> skip")
        return 1
    print("gate: Mac offline and not paused -> run")
    return 0


def run() -> int:
    LOGS.mkdir(exist_ok=True)
    DATA.mkdir(exist_ok=True)
    os.environ.setdefault("JOBBOT_SITE", "cloud")
    state_sync.pull()
    for p in DATA.glob("STOP*"):
        p.unlink()
    log_path = LOGS / "cloud-run.log"
    started = time.time()
    deadline = started + RUN_MINUTES * 60
    print(f"run: starting bot for up to {RUN_MINUTES} min (output -> {log_path.name}, kept private)")
    with open(log_path, "a") as fh:
        bot = subprocess.Popen(BOT_CMD, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT)
    heartbeat(True, started, log_path, "started")
    last_hb = last_push = last_check = time.time()
    reason = "finished"
    stop_at = None
    while bot.poll() is None:
        time.sleep(15)
        now = time.time()
        if stop_at is None and now - last_check > 90:
            last_check = now
            why = "time limit" if now > deadline else "Stop pressed on site" if paused() \
                else "Mac took over" if mac_running() else ""
            if why:
                reason = why
                print(f"run: stopping bot ({why})")
                (DATA / "STOP").touch()
                stop_at = now
        if stop_at and now - stop_at > 600:
            print("run: bot did not stop in 10 min -> terminate")
            bot.send_signal(signal.SIGTERM)
            try:
                bot.wait(60)
            except subprocess.TimeoutExpired:
                bot.kill()
        if now - last_hb > HEARTBEAT_S:
            last_hb = now
            heartbeat(stop_at is None, started, log_path)
        if now - last_push > STATE_PUSH_S:
            last_push = now
            try:
                state_sync.push()
            except Exception as exc:  # keep applying even if a sync fails
                print(f"run: state push failed: {exc}")
    print(f"run: bot exited with code {bot.returncode} ({reason}); submitted companies {_counts()}")
    for attempt in range(3):
        try:
            state_sync.push()
            break
        except Exception as exc:
            print(f"run: final state push failed ({exc}), retry")
            time.sleep(20)
    heartbeat(False, started, log_path, reason)
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "gate":
        return gate()
    if cmd == "run":
        return run()
    if cmd == "paused":
        return 0 if paused() else 1
    if cmd == "ctl" and len(sys.argv) > 2 and sys.argv[2] in ("start", "stop"):
        return 0 if publish_control(sys.argv[2]) else 1
    if cmd == "status":
        print(json.dumps({"mac_running": mac_running(), "control": control(),
                          "heartbeat": last_heartbeat()}, indent=1)[:3000])
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
