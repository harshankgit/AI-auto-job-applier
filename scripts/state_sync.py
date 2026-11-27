"""Share the bot's private state between the Mac and the GitHub cloud runner.

State = data/applications.db, data/learned_answers.json,
data/private_profile.json and data/resumes/.  It is bundled into a tar,
encrypted with openssl (AES-256-CBC, PBKDF2, key = JOBBOT_STATE_KEY) and kept
as an asset on the GitHub release tagged ``bot-state`` (never in git history).

    python scripts/state_sync.py pull   # download newest bundle, merge into data/
    python scripts/state_sync.py push   # pull+merge, then upload the merged bundle

Merging never loses rows: attempts are unioned on
(candidate_id, job_key, status, applied_at); learned answers and the private
profile are unioned with local values winning; missing resumes are copied.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPO = os.environ.get("JOBBOT_REPO", "harshankgit/AI-auto-job-applier")
TAG = "bot-state"
API = f"https://api.github.com/repos/{REPO}"
KEEP = 3


def _env_file(name: str) -> str:
    env = ROOT / "web" / ".env.local"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return ""


def _token() -> str:
    tok = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    f = DATA / "gh_token"
    if not tok and f.exists():
        tok = f.read_text().strip()
    if not tok:
        sys.exit("state_sync: no GitHub token (GH_TOKEN or data/gh_token)")
    return tok


def _key() -> str:
    key = os.environ.get("JOBBOT_STATE_KEY") or _env_file("JOBBOT_STATE_KEY")
    if not key:
        sys.exit("state_sync: no JOBBOT_STATE_KEY")
    return key


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"Authorization": f"Bearer {_token()}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=120, follow_redirects=True)


def _release(c: httpx.Client) -> dict:
    r = c.get(f"{API}/releases/tags/{TAG}")
    if r.status_code == 200:
        return r.json()
    r = c.post(f"{API}/releases", json={
        "tag_name": TAG, "name": "bot state (encrypted)", "prerelease": True,
        "body": "Encrypted bot state shared by the Mac and the cloud runner. Do not delete."})
    r.raise_for_status()
    return r.json()


def _openssl(mode: str, src: Path, dst: Path, key: str) -> None:
    args = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000",
            "-md", "sha256", "-in", str(src), "-out", str(dst), "-pass", "env:JOBBOT_SK"]
    args.insert(3, "-d" if mode == "dec" else "-salt")
    subprocess.run(args, check=True, env={**os.environ, "JOBBOT_SK": key})


# ---------------------------------------------------------------- merge
def _merge_db(remote_db: Path) -> int:
    local_db = DATA / "applications.db"
    if not local_db.exists():
        shutil.copy2(remote_db, local_db)
        return -1
    sys.path.insert(0, str(ROOT))
    from jobbot import store  # ensures schema + migrated columns
    store.init()
    conn = sqlite3.connect(local_db, timeout=60)
    conn.execute("ATTACH DATABASE ? AS r", (str(remote_db),))
    lcols = [r[1] for r in conn.execute("PRAGMA main.table_info(attempts)")]
    rcols = {r[1] for r in conn.execute("PRAGMA r.table_info(attempts)")}
    cols = [c for c in lcols if c != "id" and c in rcols]
    cl = ", ".join(cols)
    before = conn.execute("SELECT COUNT(*) FROM main.attempts").fetchone()[0]
    conn.execute(f"""
        INSERT INTO main.attempts ({cl})
        SELECT {cl} FROM r.attempts ra
        WHERE NOT EXISTS (
            SELECT 1 FROM main.attempts la
            WHERE la.candidate_id = ra.candidate_id AND la.job_key = ra.job_key
              AND la.status = ra.status
              AND IFNULL(la.applied_at, '') = IFNULL(ra.applied_at, ''))
        ORDER BY ra.applied_at""")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM main.attempts").fetchone()[0]
    conn.execute("DETACH DATABASE r")
    conn.close()
    return after - before


def _merge_json(remote: Path, local: Path) -> None:
    if not remote.exists():
        return
    try:
        rd = json.loads(remote.read_text())
    except Exception:
        return
    ld = {}
    if local.exists():
        try:
            ld = json.loads(local.read_text())
        except Exception:
            ld = {}

    def union(a, b):  # a = local (wins), b = remote
        if isinstance(a, dict) and isinstance(b, dict):
            out = dict(b)
            for k, v in a.items():
                out[k] = union(v, b[k]) if k in b else v
            return out
        return a

    merged = union(ld, rd)
    if merged != ld:  # untouched file keeps its mtime (mtime drives answer-retry logic)
        local.write_text(json.dumps(merged, indent=2, ensure_ascii=False))


def _merge_tree(extracted: Path) -> dict:
    out = {}
    db = extracted / "applications.db"
    if db.exists():
        out["new_rows"] = _merge_db(db)
    for name in ("learned_answers.json", "private_profile.json"):
        _merge_json(extracted / name, DATA / name)
    # boards discovered on another machine: take the newer list so the cloud runner
    # applies on them right away instead of re-probing first
    boards = extracted / "discovered_boards.yaml"
    if boards.exists() and (not (DATA / boards.name).exists()
                            or boards.stat().st_mtime > (DATA / boards.name).stat().st_mtime):
        shutil.copy2(boards, DATA / boards.name)
    res = extracted / "resumes"
    if res.is_dir():
        (DATA / "resumes").mkdir(parents=True, exist_ok=True)
        for f in res.iterdir():
            dst = DATA / "resumes" / f.name
            if f.is_file() and not dst.exists():
                shutil.copy2(f, dst)
    return out


# ---------------------------------------------------------------- bundle
def _bundle(tmp: Path) -> Path:
    stage = tmp / "stage"
    stage.mkdir()
    src = sqlite3.connect(DATA / "applications.db", timeout=60)
    dst = sqlite3.connect(stage / "applications.db")
    src.backup(dst)
    dst.close()
    src.close()
    for name in ("learned_answers.json", "private_profile.json", "discovered_boards.yaml"):
        if (DATA / name).exists():
            shutil.copy2(DATA / name, stage / name)
    if (DATA / "resumes").is_dir():
        shutil.copytree(DATA / "resumes", stage / "resumes")
    tar = tmp / "state.tar.gz"
    with tarfile.open(tar, "w:gz") as t:
        for p in stage.iterdir():
            t.add(p, arcname=p.name)
    enc = tmp / "state.bin"
    _openssl("enc", tar, enc, _key())
    return enc


def pull() -> dict:
    DATA.mkdir(exist_ok=True)
    with _client() as c, tempfile.TemporaryDirectory() as td:
        rel = _release(c)
        assets = sorted((a for a in rel.get("assets", []) if a["name"].startswith("state-")),
                        key=lambda a: a["name"], reverse=True)
        if not assets:
            print("state_sync: no remote state yet")
            return {"remote": None}
        a = assets[0]
        tmp = Path(td)
        r = c.get(a["url"], headers={"Accept": "application/octet-stream"})
        r.raise_for_status()
        (tmp / "state.bin").write_bytes(r.content)
        _openssl("dec", tmp / "state.bin", tmp / "state.tar.gz", _key())
        ex = tmp / "x"
        with tarfile.open(tmp / "state.tar.gz") as t:
            t.extractall(ex, filter="data")
        info = _merge_tree(ex)
        info["remote"] = a["name"]
        print(f"state_sync: pulled {a['name']} -> {info}")
        return info


def push() -> dict:
    info = pull()
    with _client() as c, tempfile.TemporaryDirectory() as td:
        rel = _release(c)
        enc = _bundle(Path(td))
        name = f"state-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.environ.get('JOBBOT_SITE', 'mac')}.bin"
        up = rel["upload_url"].split("{")[0]
        r = c.post(up, params={"name": name}, content=enc.read_bytes(),
                   headers={"Content-Type": "application/octet-stream"})
        r.raise_for_status()
        assets = sorted((a for a in _release(c).get("assets", []) if a["name"].startswith("state-")),
                        key=lambda a: a["name"], reverse=True)
        for old in assets[KEEP:]:
            c.delete(f"{API}/releases/assets/{old['id']}")
        print(f"state_sync: pushed {name} ({enc.stat().st_size // 1024} KB)")
        info["pushed"] = name
        return info


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "pull":
        pull()
    elif cmd == "push":
        push()
    else:
        sys.exit(__doc__)
