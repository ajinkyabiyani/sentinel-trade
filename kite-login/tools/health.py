"""Telemetry probe (Telemetry_DevOps_Agent): is the app up, fast and its background engines alive?

Read-only: GET requests to the local backend / frontend and process stats. Never prints a credential or token.
Usage (from C:\\Algo\\kite-login):  python tools/health.py [--log PATH] [--out reports/app_health/telemetry.json]
"""
from __future__ import annotations

import argparse
import json
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path

BACKEND, FRONTEND = "http://127.0.0.1:8000", "http://127.0.0.1:5173"
ENDPOINTS = ["/api/session", "/api/momentum", "/api/portfolio", "/api/risk-settings", "/api/multi-agent-team",
             "/api/multi-agent-team-2", "/api/news?source=all&limit=20", "/api/market/indices"]
SLOW_MS = 1500


def get(url: str, timeout: float = 20.0) -> tuple[int, float, bytes]:
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read()
            return r.status, (time.perf_counter() - t0) * 1000, body
    except urllib.error.HTTPError as e:
        return e.code, (time.perf_counter() - t0) * 1000, b""
    except Exception:  # noqa: BLE001 - down / timeout
        return 0, (time.perf_counter() - t0) * 1000, b""


def engines() -> list[dict]:
    """Background loops report progress / error fields; surface stuck or failing ones."""
    out = []
    for name, path, pick in [
        ("Momentum sweep", "/api/momentum", lambda d: ((d.get("progress") or {}).get("error"), d.get("built"))),
    ]:
        code, _, body = get(BACKEND + path)
        try:
            err, asof = pick(json.loads(body or b"{}"))
        except ValueError:
            err, asof = "unreadable response", None
        out.append({"engine": name, "http": code, "error": err or "", "last_update": asof})
    return out


def processes() -> list[dict]:
    try:
        import psutil
    except ImportError:
        return [{"note": "psutil not installed"}]
    out = []
    for p in psutil.process_iter(["pid", "name", "cmdline", "memory_info", "cpu_percent", "create_time"]):
        cmd = " ".join(p.info.get("cmdline") or [])
        if ("uvicorn" in cmd and "main:app" in cmd) or ("vite" in cmd and "node" in (p.info.get("name") or "").lower()):
            out.append({"pid": p.info["pid"], "role": "backend" if "uvicorn" in cmd else "frontend",
                        "rss_mb": round(p.info["memory_info"].rss / 1e6, 1) if p.info.get("memory_info") else None,
                        "cpu_pct": p.cpu_percent(interval=0.2),
                        "up_min": round((time.time() - p.info["create_time"]) / 60, 1)})
    return out


def log_errors(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {"scanned": False}
    lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()[-5000:]
    errs = [l for l in lines if re.search(r"\b(ERROR|Traceback|Exception)\b", l)]
    http5 = [l for l in lines if re.search(r'" 5\d\d ', l)]
    groups: dict[str, int] = {}
    for l in errs:
        key = re.sub(r"\d+", "#", l.strip())[:120]
        groups[key] = groups.get(key, 0) + 1
    return {"scanned": True, "lines": len(lines), "errors": len(errs), "http_5xx": len(http5),
            "top": sorted(groups.items(), key=lambda kv: -kv[1])[:10]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log")
    ap.add_argument("--out", default="reports/app_health/telemetry.json")
    a = ap.parse_args()
    eps = []
    for e in ENDPOINTS:
        code, ms, body = get(BACKEND + e)
        eps.append({"endpoint": e, "http": code, "ms": round(ms), "kb": round(len(body) / 1024, 1), "slow": ms > SLOW_MS})
    fcode, fms, _ = get(FRONTEND + "/")
    rep = {"at": datetime.now().isoformat(timespec="seconds"), "frontend": {"http": fcode, "ms": round(fms)},
           "endpoints": eps, "engines": engines(), "processes": processes(), "log": log_errors(a.log)}
    rep["issues"] = ([f"{x['endpoint']} returned {x['http']}" for x in eps if x["http"] not in (200, 401)]
                     + [f"{x['endpoint']} slow: {x['ms']} ms" for x in eps if x["slow"]]
                     + [f"{x['engine']} error: {x['error'][:120]}" for x in rep["engines"] if x["error"]]
                     + ([] if fcode == 200 else [f"frontend returned {fcode}"]))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(rep, indent=1), encoding="utf-8")
    print(json.dumps({"issues": rep["issues"], "slowest": sorted(eps, key=lambda x: -x["ms"])[:3],
                      "processes": rep["processes"]}, indent=1))


if __name__ == "__main__":
    main()
