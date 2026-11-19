"""Run a Locust production test end-to-end and store the results for /admin/production-test.

    python scripts/run_loadtest.py --mode stress                       # 10-minute step load 25 → 250 users
    python scripts/run_loadtest.py --mode bughunt -u 120 -t 5m         # constant load with 3× adversarial traffic
    python scripts/run_loadtest.py --import-only results/<stamp>       # re-import an earlier run

Steps: (optional) seed → Locust headless (CSV + HTML) → DB/log/pool invariants → LoadTestRun row.
Requires the app running locally with TEST_MODE=true and its log at var/logs/server.log.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import platform
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests" / "load"))
LOAD = ROOT / "tests" / "load"
RESULTS = LOAD / "results"
LOG = ROOT / "var" / "logs" / "server.log"
PCTS = ["50%", "66%", "75%", "80%", "90%", "95%", "98%", "99%", "99.9%", "99.99%", "100%"]


def _num(v: str | None) -> float | None:
    try:
        return float(v) if v not in (None, "", "N/A") else None
    except ValueError:
        return None


def parse(prefix: Path) -> dict:
    """Locust CSVs → endpoints, failures, 5-second timeline."""
    stats = list(csv.DictReader(open(f"{prefix}_stats.csv", encoding="utf-8")))
    endpoints, total = [], None
    for r in stats:
        row = {"method": r["Type"], "name": r["Name"], "requests": int(r["Request Count"]), "failures": int(r["Failure Count"]),
               "median": _num(r["Median Response Time"]), "avg": _num(r["Average Response Time"]), "min": _num(r["Min Response Time"]),
               "max": _num(r["Max Response Time"]), "size": _num(r["Average Content Size"]), "rps": _num(r["Requests/s"]),
               **{f"p{k.rstrip('%').replace('.', '_')}": _num(r.get(k)) for k in ("50%", "75%", "90%", "95%", "99%", "99.9%", "100%")}}
        if r["Name"] == "Aggregated":
            total = row
        else:
            endpoints.append(row)
    failures = [{"method": r["Method"], "name": r["Name"], "error": r["Error"][:300], "count": int(r["Occurrences"])}
                for r in csv.DictReader(open(f"{prefix}_failures.csv", encoding="utf-8"))]
    hist = [r for r in csv.DictReader(open(f"{prefix}_stats_history.csv", encoding="utf-8")) if r["Name"] == "Aggregated"]
    timeline, t0 = [], None
    bucket: list[dict] = []
    for r in hist:
        ts = int(r["Timestamp"])
        t0 = t0 if t0 is not None else ts
        bucket.append(r)
        if len(bucket) == 5 or r is hist[-1]:
            last = bucket[-1]
            timeline.append({"t": ts - t0, "ts": ts, "users": int(last["User Count"]),
                             "rps": round(sum(_num(b["Requests/s"]) or 0 for b in bucket) / len(bucket), 2),
                             "fail_s": round(sum(_num(b["Failures/s"]) or 0 for b in bucket) / len(bucket), 2),
                             "p50": _num(last["50%"]), "p95": _num(last["95%"]), "p99": _num(last["99%"]),
                             "requests": int(last["Total Request Count"]), "failures": int(last["Total Failure Count"])})
            bucket = []
    return {"total": total, "endpoints": endpoints, "failures": failures, "timeline": timeline, "t0": t0}


def summarise(parsed: dict, server: list[dict], invariants: list[dict], mode: str, duration: int) -> dict:
    tot, tl = parsed["total"], parsed["timeline"]
    req, fail = tot["requests"], tot["failures"]
    busy = [p for p in tl if p["users"] > 0]
    peak = max(busy, key=lambda p: p["rps"]) if busy else None
    top = max((p["users"] for p in tl), default=0)
    at_peak = [p for p in tl if p["users"] == top and p["p95"] is not None]
    srv_cpu = [s["cpu"] for s in server if "cpu" in s]
    srv_rss = [s["rss_mb"] for s in server if "rss_mb" in s]
    lag = [s["loop_lag_max_ms"] for s in server if "loop_lag_max_ms" in s]
    inv = [i for i in invariants if i["kind"] == "invariant"]
    five_xx = sum(f["count"] for f in parsed["failures"] if "HTTP 5" in f["error"] or "HTTP 0" in f["error"])
    return {"requests": req, "failures": fail, "error_rate": round(100 * fail / req, 3) if req else 0, "five_xx": five_xx,
            "avg_rps": round(req / duration, 1) if duration else 0, "peak_rps": peak["rps"] if peak else 0,
            "peak_rps_users": peak["users"] if peak else 0, "p50": tot["p50"], "p95": tot["p95"], "p99": tot["p99"], "max": tot["max"],
            "p95_at_peak_users": round(sorted(p["p95"] for p in at_peak)[len(at_peak) // 2], 0) if at_peak else None,
            "peak_users": top, "server_cpu_peak": max(srv_cpu, default=None), "server_cpu_avg": round(sum(srv_cpu) / len(srv_cpu), 1) if srv_cpu else None,
            "server_rss_peak": max(srv_rss, default=None), "loop_lag_peak_ms": max(lag, default=None),
            "invariants_total": len(inv), "invariants_passed": sum(1 for i in inv if i["ok"]), "mode": mode}


def environment(mode: str, args: argparse.Namespace) -> dict:
    import psutil

    from app.config import settings

    return {"mode": mode, "host": "127.0.0.1:8088 (same machine as Locust)", "python": platform.python_version(),
            "os": f"{platform.system()} {platform.release()}", "cpu_cores": psutil.cpu_count(logical=True),
            "ram_gb": round(psutil.virtual_memory().total / 2**30, 1), "workers": max(1, settings.WEB_CONCURRENCY),
            "db_pool": f"{settings.DB_POOL_SIZE}+{settings.DB_POOL_SIZE} per worker",
            "ai_concurrency": settings.AI_CONCURRENCY, "antivirus": "ClamAV (clamd INSTREAM)" if settings.CLAMAV_HOST else "off",
            "locust": __import__("locust").__version__,
            "shape": "25 → 75 → 150 → 200 → 250 users (steps at 1, 2.5, 4.5, 6.5 min), 10 min" if mode == "stress"
            else f"{args.users} users constant, {args.duration}, adversarial share ×3",
            "user_classes": "Visitor 3 · Worker 5 · Owner 2 · Buyer 3 · Chaos 1 (×3 in bug hunt) · Socket 1 · DeviceSwitch 1 · AI 2 fixed"}


async def _lift_test_bans() -> int:
    from sqlalchemy import text

    from app.database import engine

    async with engine.begin() as conn:
        n = (await conn.execute(text(r"UPDATE ip_bans SET lifted_at = now() WHERE lifted_at IS NULL AND ip ~ '^10\.[1-7][0-9]\.'"))).rowcount
    await engine.dispose()
    return n or 0


def lift_test_bans() -> None:
    """Earlier runs ban their own attacker IPs for 24 h. Lift ONLY the load-test ranges (10.10.x.x–10.79.x.x) so the next
    run's attackers start fresh; the app's 30-second ban-cache refresh then drops them."""
    import redis

    from app.config import settings

    n = asyncio.run(_lift_test_bans())
    r = redis.Redis.from_url(settings.REDIS_URL)
    for pattern in ("ks:ipban:10.*", "ks:rl429:10.*", "ks:rl:*:ip:10.*", "ks:strikes:ip:10.*", "ks:judged:ip:10.*",
                    "ks:ipusers:10.*", "ks:msgs:ip:10.*"):
        for k in r.scan_iter(pattern, count=1000):
            r.delete(k)
    if n:
        print(f"lifted {n} load-test IP bans; waiting 35 s for the app's ban cache to refresh")
        time.sleep(35)


def periodic_check(started: datetime, at: int) -> dict:
    """Database invariants in the middle of the run (a bug is caught when it happens, not three hours later)."""
    sys.path.insert(0, str(LOAD))
    from check_invariants import _db_checks

    try:
        res = asyncio.run(_db_checks(started))
    except Exception as exc:  # the check itself must never stop the soak
        return {"t": at, "error": str(exc)[:200]}
    failed = [r["name"] for r in res if r["kind"] == "invariant" and not r["ok"]]
    print(f"[{at // 60:>4} min] invariants: {'ALL OK' if not failed else 'FAILED ' + ', '.join(failed)}", flush=True)
    return {"t": at, "failed": failed, "values": {r["name"]: r["value"] for r in res}}


def downsample(points: list[dict], limit: int = 720) -> list[dict]:
    """Long runs: average consecutive points so charts stay light (max-type fields keep their maximum)."""
    if len(points) <= limit:
        return points
    k = -(-len(points) // limit)
    out = []
    for i in range(0, len(points), k):
        chunk = points[i:i + k]
        row = dict(chunk[0])
        for key in {kk for p in chunk for kk, v in p.items() if isinstance(v, (int, float)) and kk not in ("t", "ts")}:
            vals = [p[key] for p in chunk if isinstance(p.get(key), (int, float))]
            if vals:
                row[key] = max(vals) if key in ("users", "loop_lag_max_ms", "p99", "max") or key.endswith("_max") else round(sum(vals) / len(vals), 2)
        out.append(row)
    return out


async def store(row: dict) -> str:
    from app.database import SessionLocal, engine
    from app.models import LoadTestRun

    async with SessionLocal() as db:
        run = LoadTestRun(**row)
        db.add(run)
        await db.commit()
        rid = str(run.id)
    await engine.dispose()
    return rid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["stress", "bughunt", "soak"], default="stress")
    ap.add_argument("--soak-seconds", type=int, default=10800, help="soak length (default 3 h)")
    ap.add_argument("--check-every", type=int, default=900, help="soak: run the database invariants every N seconds")
    ap.add_argument("-u", "--users", type=int, default=120)
    ap.add_argument("-r", "--spawn-rate", type=int, default=10)
    ap.add_argument("-t", "--duration", default="5m")
    ap.add_argument("--seed", action="store_true", help="seed fresh load-test data first")
    ap.add_argument("--label", default="")
    ap.add_argument("--import-only", help="results/<stamp> prefix of an earlier run")
    ap.add_argument("--host", default="http://127.0.0.1:8088", help="server under test")
    ap.add_argument("--docker", help="container name: scan its logs (docker logs) instead of var/logs/server.log")
    a = ap.parse_args()
    RESULTS.mkdir(parents=True, exist_ok=True)
    if a.seed:
        subprocess.run([sys.executable, str(LOAD / "seed_loadtest.py"), "--owners", "40", "--workers", "500"], check=True)

    if not a.import_only:
        lift_test_bans()
    if a.import_only:
        prefix = Path(a.import_only)
        meta = json.loads(Path(f"{prefix}_meta.json").read_text("utf-8"))
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        prefix = RESULTS / stamp
        offset = LOG.stat().st_size if LOG.exists() else 0
        started = datetime.now(timezone.utc)
        cmd = [sys.executable, "-m", "locust", "-f", str(LOAD / "locustfile.py"), "--headless", "--csv", str(prefix), "--csv-full-history",
               "--html", f"{prefix}.html", "--loglevel", "WARNING", "--stop-timeout", "20"]
        if a.mode == "bughunt":
            cmd += ["-u", str(a.users), "-r", str(a.spawn_rate), "-t", a.duration]
        env = {**os.environ, "KS_MODE": a.mode, "KS_STAMP": stamp, "KS_RESULTS": str(RESULTS), "PYTHONIOENCODING": "utf-8",
               "KS_DURATION": str(a.soak_seconds),
               "KS_HOST": a.host, "KS_REMOTE": "1" if a.docker else "0",
               "PYTHONUTF8": "1"}  # non-ASCII failure messages (e.g. Bengali) crashed Locust's CSV writer under cp1252
        print("running:", " ".join(cmd[2:]))
        t0 = time.time()
        proc = subprocess.Popen(cmd, env=env, cwd=str(ROOT))
        periodic = []
        next_check = t0 + a.check_every
        while proc.poll() is None:              # long runs: check the database while the load is ON, not only after
            time.sleep(5)
            if a.mode == "soak" and time.time() >= next_check:
                next_check += a.check_every
                periodic.append(periodic_check(started, round(time.time() - t0)))
        Path(f"{prefix}_periodic.json").write_text(json.dumps(periodic), "utf-8")
        meta = {"stamp": stamp, "started": started.isoformat(), "finished": datetime.now(timezone.utc).isoformat(),
                "duration": round(time.time() - t0), "log_offset": offset, "mode": a.mode, "users": a.users, "dur": a.duration,
                "host": a.host, "docker": a.docker}
        if a.docker:   # the container's log for exactly this run
            logs = subprocess.run(["docker", "logs", "--since", started.isoformat(), a.docker], capture_output=True)
            Path(f"{prefix}_server.log").write_bytes(logs.stdout + logs.stderr)
        Path(f"{prefix}_meta.json").write_text(json.dumps(meta), "utf-8")

    from check_invariants import run as run_invariants

    host = meta.get("host") or "http://127.0.0.1:8088"
    log_path, log_offset = (Path(f"{prefix}_server.log"), 0) if meta.get("docker") else (LOG, meta["log_offset"])
    invariants = run_invariants(datetime.fromisoformat(meta["started"]), log_path, log_offset, Path(f"{prefix}_checks.json"), host=host)
    parsed = parse(prefix)
    server = json.loads(Path(f"{prefix}_server.json").read_text("utf-8")) if Path(f"{prefix}_server.json").exists() else []
    t0 = parsed["t0"] or 0
    for s in server:  # align sampler points with the Locust timeline
        s["t"] = round(s.get("ts", t0) - t0, 1) if "ts" in s else s["t"]
    findings_path = LOAD / "findings.json"
    findings = json.loads(findings_path.read_text("utf-8")) if findings_path.exists() else []
    duration = int(meta["duration"])
    summary = summarise(parsed, server, invariants, meta["mode"], duration)
    try:
        import requests

        summary["workers"] = requests.get(host + "/internal/metrics", timeout=5).json().get("workers", 1)
    except Exception:
        summary["workers"] = None
    periodic_path = Path(f"{prefix}_periodic.json")
    if periodic_path.exists():
        summary["periodic"] = json.loads(periodic_path.read_text("utf-8"))
    inv_ok = all(i["ok"] for i in invariants if i["kind"] == "invariant") and not any(p.get("failed") for p in summary.get("periodic", []))
    passed = inv_ok and summary["five_xx"] == 0 and summary["error_rate"] < 1.0
    label = a.label or (f"10-minute step load to {summary['peak_users']} users" if meta["mode"] == "stress"
                        else f"{duration / 3600:.1f}-hour soak, up to {summary['peak_users']} users" if meta["mode"] == "soak"
                        else f"Bug hunt — {meta.get('users')} users, {meta.get('dur')}")
    row = {"label": label, "kind": {"stress": "stress", "soak": "soak"}.get(meta["mode"], "bug_hunt"),
           "started_at": datetime.fromisoformat(meta["started"]), "finished_at": datetime.fromisoformat(meta["finished"]),
           "duration_s": duration, "peak_users": summary["peak_users"], "total_requests": summary["requests"],
           "total_failures": summary["failures"], "passed": passed, "summary": summary, "timeline": downsample(parsed["timeline"]),
           "endpoints": sorted(parsed["endpoints"], key=lambda e: -(e["requests"])), "failures": parsed["failures"],
           "server": downsample(server), "invariants": invariants, "findings": findings,
           "config": {**environment(meta["mode"], a), **({"workers": summary["workers"]} if summary.get("workers") else {}),
                      **({"host": f"{host} (Docker / Linux container)"} if meta.get("docker") else {})}}
    rid = asyncio.run(store(row))
    print(json.dumps(summary, indent=1))
    for i in invariants:
        if i["kind"] == "invariant":
            print(("PASS " if i["ok"] else "FAIL ") + f"{i['name']:<38} {i['value']}")
            for s in i.get("sample", []):
                print("        ", s)
    print(f"\nstored run {rid} → http://localhost:8088/admin/production-test?run={rid}   passed={passed}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
