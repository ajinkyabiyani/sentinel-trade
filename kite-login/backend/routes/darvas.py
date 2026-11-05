"""Darvas scans: runs darvas_algo.py as a child process and reads its CSV reports."""
from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
import csv
import json
import os
import re
import subprocess
import sys
import threading

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from core import DARVAS_SCRIPT, IST, REPORTS_DIR, app, log, store
from strategies import momentum


# ---------------------------------------------------------------------------
# Darvas: run darvas_algo.py rank, read its CSV reports
# ---------------------------------------------------------------------------
TEXT_COLS = {"symbol", "exchange", "segment", "signal", "breakout_date", "breakout_time", "status", "filters",
             "indices", "box_since", "index", "date", "sector", "stop_basis", "status", "outcome", "signal_date",
             "entry_date", "t1_date", "t2_date", "stop_date", "exit_date"}
REPORT_RE = re.compile(r"^breakouts_(\d{8}_\d{4})\.csv$")


def _cell(col: str, v: str):
    if v == "":
        return None
    if col in TEXT_COLS:
        return v
    if v in ("True", "False"):
        return v == "True"
    try:
        f = float(v)
        return int(f) if f.is_integer() and col.endswith(("token", "qty", "rank", "members", "bars_ago")) else f
    except ValueError:
        return v


def _read_report(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return [{k: _cell(k, v) for k, v in row.items()} for row in csv.DictReader(fh)]


def _stamps() -> list[str]:
    if not REPORTS_DIR.exists():
        return []
    return sorted((m.group(1) for p in REPORTS_DIR.iterdir() if (m := REPORT_RE.match(p.name))), reverse=True)


@app.get("/api/darvas/results")
def darvas_results(stamp: str | None = Query(None, pattern=r"^\d{8}_\d{4}$")):
    stamps = _stamps()
    if not stamps:
        return {"stamp": None, "stamps": [], "breakouts": [], "ready": [], "indices": []}
    s = stamp if stamp in stamps else stamps[0]
    membership: dict[str, list[str]] = {}   # the CSV "indices" column is shortened to 4 names
    for r in _read_report(REPORTS_DIR / "index_constituents.csv"):
        if r.get("symbol") and r.get("index"):
            membership.setdefault(str(r["symbol"]), []).append(r["index"])
    sectors_file = REPORTS_DIR / f"sector_rotation_{s}.json"
    try:
        sectors = json.loads(sectors_file.read_text(encoding="utf-8")) if sectors_file.exists() else []
    except ValueError:
        sectors = []
    lead_file = REPORTS_DIR / f"leadership_{s}.json"
    try:
        leadership = json.loads(lead_file.read_text(encoding="utf-8")) if lead_file.exists() else None
    except ValueError:
        leadership = None
    return {"stamp": s, "stamps": stamps[:30], "membership": membership, "sectors": sectors, "leadership": leadership,
            "generated_at": datetime.strptime(s, "%Y%m%d_%H%M").replace(tzinfo=IST).isoformat(),
            "breakouts": _read_report(REPORTS_DIR / f"breakouts_{s}.csv"),
            "ready": _read_report(REPORTS_DIR / f"darvas_ready_{s}.csv"),
            "indices": _read_report(REPORTS_DIR / f"index_summary_{s}.csv"),
            "outcomes": _read_report(REPORTS_DIR / f"outcomes_{s}.csv")}


# The five indices the scanner and dashboard track (same list as CORE_INDICES in darvas_algo.py).
CORE_INDICES = ["BSE:SENSEX", "NSE:NIFTY 50", "NSE:NIFTY BANK", "NSE:NIFTY MIDCAP 100", "NSE:NIFTY SMLCAP 100"]


@app.get("/api/darvas/universes")
def darvas_universes():
    return {"universes": ["top500", "core", *(k.split(":", 1)[1] for k in CORE_INDICES)]}


TOP500_FILE = REPORTS_DIR / "darvas_universe_top500.csv"


def _top500() -> str:
    """Quant Darvas universe: the 500 most liquid NSE stocks (median 20-day turnover, from the daily momentum sweep),
    re-ranked monthly. Returns the list's path for darvas_algo.py, or "core" until a sweep exists."""
    import csv
    try:
        fresh = TOP500_FILE.exists() and datetime.now().timestamp() - TOP500_FILE.stat().st_mtime < 30 * 86400
    except OSError:
        fresh = False
    if not fresh:
        d = momentum.latest()
        rows = sorted((x for x in (d or {}).get("stocks", []) if x.get("turnover_cr")), key=lambda x: -x["turnover_cr"])[:500]
        if len(rows) < 300:
            return str(TOP500_FILE) if TOP500_FILE.exists() else "core"
        REPORTS_DIR.mkdir(exist_ok=True)
        with TOP500_FILE.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["symbol", "exchange", "name"])
            for x in rows:
                w.writerow([x["symbol"], "NSE", x.get("name") or ""])
        log.info("Darvas universe re-ranked: top %d by turnover (sweep of %s)", len(rows), d.get("day"))
    return str(TOP500_FILE)


class DarvasRunBody(BaseModel):
    universe: str = Field("top500", min_length=1, max_length=60, pattern=r"^[A-Za-z0-9 &:/().-]+$")
    years: float = Field(2.0, ge=1.3, le=10)
    window: int = Field(20, ge=1, le=250)
    times: int = Field(50, ge=0, le=500)
    no_indices: bool = False


class ScriptRunner:
    """One background darvas_algo.py job at a time; keeps its output for the progress view.
    The access token reaches the child through the environment and is masked out of the captured log."""

    def __init__(self, busy_msg: str):
        self.busy_msg = busy_msg
        self.on_exit = None
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.lines: deque[str] = deque(maxlen=300)
        self.started_at = self.finished_at = None
        self.exit_code: int | None = None
        self.params: dict = {}
        self.stage = ""
        self.progress: tuple[int, int] | None = None
        self._mask = ""

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, body: DarvasRunBody, api_key: str, access_token: str, auto: bool = False) -> None:
        cmd = ["rank", "--universe", _top500() if body.universe == "top500" else body.universe, "--years", str(body.years), "--window", str(body.window),
               "--times", str(body.times), "--out-dir", str(REPORTS_DIR)]
        if body.no_indices:
            cmd.append("--no-indices")
        self.launch(cmd, api_key, access_token, {**body.model_dump(), "auto": auto},
                    "Starting automatic scan" if auto else "Starting")

    def launch(self, args: list[str], api_key: str, access_token: str, params: dict, stage: str, on_exit=None,
               script: Path = DARVAS_SCRIPT) -> None:
        with self._lock:
            if self.running:
                raise HTTPException(409, self.busy_msg)
            cmd = [sys.executable, "-u", str(script), *args]
            env = {**os.environ, "KITE_API_KEY": api_key, "KITE_ACCESS_TOKEN": access_token,
                   "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            self.lines.clear()
            self.params, self.progress, self.exit_code, self.stage, self.on_exit = params, None, None, stage, on_exit
            self.started_at, self.finished_at, self._mask = datetime.now(IST).isoformat(), None, access_token
            self.proc = subprocess.Popen(cmd, cwd=Path(script).parent, env=env, stdout=subprocess.PIPE,
                                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            line = raw.rstrip().replace(self._mask, "***") if self._mask else raw.rstrip()
            if not line:
                continue
            m = re.match(r"\s*\[(\d+)/(\d+)\]\s*(.*)", line)
            if m:
                self.progress = (int(m.group(1)), int(m.group(2)))
                if m.group(3).startswith("param set"):
                    self.stage = "Walk-forward: testing parameter sets"
                elif self.stage.startswith("Starting"):
                    self.stage = "Loading price history"
            elif line.startswith(("Fetching", "Looking up", "Universe")) or "symbols ready" in line:
                self.stage, self.progress = line, None
            self.lines.append(line)
        self.exit_code = proc.wait()
        self.finished_at = datetime.now(IST).isoformat()
        self.stage = "Finished" if self.exit_code == 0 else f"Failed (exit code {self.exit_code})"
        if self.on_exit:
            try:
                self.on_exit(self.exit_code, list(self.lines))
            except Exception:
                log.exception("job exit hook failed")

    def stop(self) -> None:
        if self.running:
            self.proc.terminate()

    def status(self) -> dict:
        return {"running": self.running, "started_at": self.started_at, "finished_at": self.finished_at,
                "exit_code": self.exit_code, "params": self.params, "stage": self.stage,
                "progress": self.progress, "log": list(self.lines)[-80:]}


runner = ScriptRunner("A Darvas scan is already running")


def _last_close(now: datetime) -> datetime:
    """Most recent weekday 15:45 IST, when that day's candle is final. Exchange holidays aren't known here, so a
    holiday costs one extra scan (mostly served from darvas_algo's cache)."""
    d = now.replace(hour=15, minute=45, second=0, microsecond=0)
    if now < d:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def ensure_fresh_scan(d: dict | None) -> bool:
    """Start a core scan when the newest results predate the last market close. Called after login and by the
    background checker, so the Darvas data is ready without pressing "Run scan"."""
    if not d or runner.running or not DARVAS_SCRIPT.exists():
        return False
    stamps = _stamps()
    latest = datetime.strptime(stamps[0], "%Y%m%d_%H%M").replace(tzinfo=IST) if stamps else None
    if latest and latest >= _last_close(datetime.now(IST)):
        return False
    if runner.exit_code not in (None, 0) and runner.started_at and \
            datetime.fromisoformat(runner.started_at) > datetime.now(IST) - timedelta(hours=1):
        return False   # the last automatic scan failed recently: don't retry in a loop
    try:
        runner.start(DarvasRunBody(), d["api_key"], d["access_token"], auto=True)
        log.info("started automatic Darvas scan (newest results: %s)", stamps[0] if stamps else "none")
        return True
    except HTTPException:
        return False


def _auto_scan_loop() -> None:
    import time
    time.sleep(5)
    while True:
        try:
            ensure_fresh_scan(store.load())
        except Exception:
            log.exception("automatic scan check failed")
        time.sleep(600)


@app.on_event("startup")
def _start_auto_scan() -> None:
    threading.Thread(target=_auto_scan_loop, daemon=True, name="auto-scan").start()


@app.post("/api/darvas/run")
def darvas_run(body: DarvasRunBody):
    d = store.load()
    if not d:
        raise HTTPException(401, "Not logged in to Kite")
    if not DARVAS_SCRIPT.exists():
        raise HTTPException(500, f"{DARVAS_SCRIPT} not found")
    runner.start(body, d["api_key"], d["access_token"])
    return runner.status()


@app.get("/api/darvas/status")
def darvas_status():
    return runner.status()


@app.post("/api/darvas/stop")
def darvas_stop():
    runner.stop()
    return runner.status()


