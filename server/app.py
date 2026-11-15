"""SIPS portal: paste a public GitHub repo, get its SIPS design estimate.

POST /api/score {"repo": "https://github.com/owner/name"}  ->  JSON result
GET  /                                                     ->  web page
GET  /api/results?key=ADMIN_KEY                            ->  every stored run (JSON)

Each run stores the repo URL, the score and the result, in Postgres when DATABASE_URL is set,
otherwise in a local JSONL file. The repo is cloned into a temp dir, read (never executed) and deleted.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from sips.redact import redact
from sips.repo import PATTERNS, _count, scan

REPO_RX = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")
MAX_REPO_MB = 300
CLONE_TIMEOUT = 90
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
LOCAL_STORE = Path(os.environ.get("LOCAL_STORE", "/tmp/sips-runs.jsonl"))
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
EXAMPLES = {"ai-only": "AI alone", "approve-every-step": "Approve every one", "designed-loop": "Designed loop"}

app = FastAPI(title="SIPS portal", docs_url="/api/docs", redoc_url=None)
_slots = threading.Semaphore(3)          # at most 3 clones at once on a small instance
_cache: dict[str, tuple[float, dict]] = {}
_hits: dict[str, list[float]] = {}
_lock = threading.Lock()


# ------------------------------------------------------------------ storage
def _db():
    import psycopg
    return psycopg.connect(DATABASE_URL, autocommit=True)


def _init_db():
    if not DATABASE_URL:
        return
    with _db() as c:
        c.execute("""create table if not exists runs (
            id serial primary key, ts timestamptz default now(), repo text, sips real,
            verdict text, team text, ip_hash text, result jsonb)""")


def _store(repo: str, team: str, ip: str, result: dict):
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "repo": repo, "team": team,
           "sips": result.get("sips"), "verdict": result.get("verdict", ""),
           "ip_hash": hashlib.sha256(ip.encode()).hexdigest()[:12]}
    try:
        if DATABASE_URL:
            with _db() as c:
                c.execute("insert into runs (repo, sips, verdict, team, ip_hash, result) values (%s,%s,%s,%s,%s,%s)",
                          (repo, row["sips"], row["verdict"], team, row["ip_hash"], json.dumps(result)))
        else:
            with LOCAL_STORE.open("a") as f:
                f.write(json.dumps({**row, "result": result}) + "\n")
    except Exception as e:                # never fail a user's request because storage hiccuped
        print("store failed:", e, flush=True)


# ------------------------------------------------------------------ scoring
def _limited(ip: str) -> bool:
    now = time.time()
    with _lock:
        recent = [t for t in _hits.get(ip, []) if now - t < 60]
        recent.append(now)
        _hits[ip] = recent
        return len(recent) > 8


def _score_repo(url: str) -> dict:
    tmp = tempfile.mkdtemp(prefix="sips-")
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        r = subprocess.run(["git", "clone", "--quiet", "--depth", "400", "--single-branch", "--no-tags",
                            "--filter=blob:limit=2m", url, tmp + "/r"],
                           capture_output=True, text=True, timeout=CLONE_TIMEOUT, env=env)
        if r.returncode != 0:
            raise HTTPException(404, "Could not clone that repo. Is it public, and is the URL right?")
        size = sum(f.stat().st_size for f in Path(tmp).rglob("*") if f.is_file()) / 1e6
        if size > MAX_REPO_MB:
            raise HTTPException(413, f"Repo too large to scan here ({size:.0f} MB). Run it locally with pipx.")
        res = scan(tmp + "/r")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "Clone took too long. Run it locally with pipx.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _result(url, res)


def _story(res) -> dict:
    """The two questions from the talk, in plain words."""
    t1 = _count(res, "guardrail", "system-prompt-rule") + len(res.rule_files)
    t2 = _count(res, "eval-set", "feedback-capture") + len(res.test_files)
    t3, t4 = _count(res, "oversight"), _count(res, "per-step-approval")
    esc, learn, fb = _count(res, "escalation"), _count(res, "learning-loop"), _count(res, "feedback-capture")
    tests = len(res.test_files) + _count(res, "eval-set")
    autonomous = res.verdict.startswith("Fully autonomous")
    approvals_lead = bool(t4) and t4 >= t1 + t2
    if autonomous:
        time_head = "Zero human minutes, and nobody is checking."
    elif approvals_lead:
        time_head = "Most touchpoints are one click per result: that time grows with every result."
    elif t1 + t2 > t4:
        time_head = "Human time goes mostly into rules, examples and tests: it does not grow with volume."
    else:
        time_head = "A few human touchpoints, mostly low leverage."
    if autonomous:
        closest = "ai-only"
    elif approvals_lead and res.sips < 50:
        closest = "approve-every-step"
    elif res.sips >= 70:
        closest = "designed-loop"
    else:
        closest = ""
    right = [("Tests or expected answers", f"{tests} found" if tests else "none: no way to tell which results are wrong", bool(tests)),
             ("Asks a person when it is stuck", f"yes ({esc} place{'' if esc == 1 else 's'})" if esc else "no: missing information becomes a silent guess", bool(esc)),
             ("Keeps the corrections", "yes, fed back into the loop" if learn else ("collected but not reused" if fb else "no"), bool(learn))]
    ok = sum(r[2] for r in right)
    right_head = ["You can't tell which results are wrong.", "Partly checked.", "Mostly checked.", "Checked, and it learns."][ok]
    return {
        "closest": closest, "closest_name": EXAMPLES.get(closest, ""),
        "time": {"head": time_head, "steps": [
            {"name": "Rules and limits", "note": "one rule, every result", "count": t1},
            {"name": "Examples and tests", "note": "teach once, reuse", "count": t2},
            {"name": "Review as a set", "note": "spot the pattern", "count": t3},
            {"name": "Approve one by one", "note": "one click, one result", "count": t4}]},
        "right": {"head": right_head, "checks": [{"name": n, "value": v, "ok": o} for n, v, o in right]},
    }


def _result(url: str, res) -> dict:
    placement = []
    for sig, role, _, why in PATTERNS:
        ev = [f"{h.path}:{h.line}  {redact(h.text)}" for h in res.hits if h.signal == sig]
        if ev:
            placement.append({"signal": sig, "tier": str(role), "why": why, "count": len(ev), "evidence": ev[:3]})
    return {
        "repo": url,
        "sips": round(res.sips, 1),
        "uses_ai": res.uses_ai,
        "verdict": res.verdict,
        "files": res.files, "commits": res.commits, "ai_commits": res.ai_commits,
        "components": [{"key": k, "name": n, "value": round(v, 2), "why": w} for k, (n, v, w) in res.components.items()],
        "placement": placement,
        "findings": [{**f, "evidence": [redact(e) for e in f["evidence"][:2]]} for f in res.findings],
        "story": _story(res),
        "note": "Static estimate from code and git history. Nothing was executed.",
    }


# ------------------------------------------------------------------ routes
@app.on_event("startup")
def _startup():
    try:
        subprocess.run([sys.executable, str(EXAMPLES_DIR / "make_loop_examples.py")], capture_output=True, timeout=60)
    except Exception as e:
        print("examples failed:", e, flush=True)
    try:
        _init_db()
    except Exception as e:
        print("db init failed:", e, flush=True)


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/api/score")
async def api_score(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, 'Send JSON: {"repo": "https://github.com/owner/name"}')
    url = str(body.get("repo", "")).strip()
    team = str(body.get("team", "")).strip()[:80]
    if url.startswith("example:") and url[8:] in EXAMPLES:
        path = EXAMPLES_DIR / "loops" / url[8:]
        if not path.exists():
            raise HTTPException(503, "Examples are still being prepared. Try again in a few seconds.")
        return JSONResponse({**_result(url, scan(str(path))), "example": EXAMPLES[url[8:]]})
    m = REPO_RX.match(url)
    if not m:
        raise HTTPException(400, "Use a public GitHub URL like https://github.com/owner/name")
    url = f"https://github.com/{m.group(1)}/{m.group(2)}"
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")).split(",")[0].strip()
    if _limited(ip):
        raise HTTPException(429, "Too many requests. Wait a minute and try again.")

    cached = _cache.get(url.lower())
    if cached and time.time() - cached[0] < 300:
        result = cached[1]
    else:
        if not _slots.acquire(timeout=120):
            raise HTTPException(503, "Busy right now. Try again in a minute.")
        try:
            result = _score_repo(url)
        finally:
            _slots.release()
        _cache[url.lower()] = (time.time(), result)
    _store(url, team, ip, result)
    return JSONResponse(result)


@app.get("/api/results")
def api_results(key: str = ""):
    if not ADMIN_KEY or key != ADMIN_KEY:
        raise HTTPException(403, "Forbidden")
    if DATABASE_URL:
        with _db() as c:
            rows = c.execute("select ts, repo, team, sips, verdict, ip_hash, result from runs order by ts").fetchall()
        return [{"ts": str(r[0]), "repo": r[1], "team": r[2], "sips": r[3], "verdict": r[4], "ip_hash": r[5], "result": r[6]} for r in rows]
    if LOCAL_STORE.exists():
        return [json.loads(l) for l in LOCAL_STORE.read_text().splitlines() if l.strip()]
    return []


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).parent / "index.html").read_text()
