"""Keeps Sentinel Trade running: starts the backend (port 8000) and the frontend (port 5173) when they aren't answering, and starts
them again if they stop. Started at Windows login from the Startup folder, with pythonw so no window opens.

    pythonw keep_running.py           run the watchdog (one copy only: a second copy exits at once)
    python  keep_running.py --check   say what is running and what it would start, then exit

It only ever starts the two servers. It never stops or kills anything, so a server you started yourself is simply left alone.
Logs: kite-login/logs/backend.log, frontend.log and watchdog.log.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND, FRONTEND = ROOT / "backend", ROOT / "frontend"
LOGS = ROOT / "logs"
PYTHON = Path(sys.executable).with_name("python.exe")          # pythonw starts this script; the server runs under python
NPM = r"C:\Program Files\nodejs\npm.cmd"
LOCK_PORT = 47613                                               # held while the watchdog runs, so only one copy can
EVERY = 20                                                      # seconds between checks
GRACE = 90                                                      # seconds a server is given to come up before it's started again
MAX_LOG = 5_000_000
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SERVERS = {
    "backend": {"port": 8000, "cwd": BACKEND,
                "cmd": [str(PYTHON), "-m", "uvicorn", "main:app", "--app-dir", str(BACKEND), "--reload", "--reload-dir", str(BACKEND),
                        "--host", "127.0.0.1", "--port", "8000", "--timeout-graceful-shutdown", "3"]},
    "frontend": {"port": 5173, "cwd": FRONTEND, "cmd": [NPM, "run", "dev", "--", "--strictPort"]},
}


def up(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def log(msg: str) -> None:
    LOGS.mkdir(exist_ok=True)
    with (LOGS / "watchdog.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")


def _logfile(name: str):
    LOGS.mkdir(exist_ok=True)
    path = LOGS / f"{name}.log"
    if path.exists() and path.stat().st_size > MAX_LOG:
        path.replace(path.with_suffix(".old.log"))
    return path.open("ab")


def start(name: str) -> subprocess.Popen:
    s = SERVERS[name]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out = _logfile(name)
    out.write(f"\n--- started by the watchdog at {datetime.now():%Y-%m-%d %H:%M:%S} ---\n".encode())
    out.flush()
    p = subprocess.Popen(s["cmd"], cwd=s["cwd"], env=env, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
    log(f"started {name} (pid {p.pid})")
    return p


def check() -> None:
    for name, s in SERVERS.items():
        print(f"{name:9s} port {s['port']}: {'answering' if up(s['port']) else 'down, would start'}")
    print("python  :", PYTHON, "exists" if PYTHON.exists() else "MISSING")
    print("npm     :", NPM, "exists" if Path(NPM).exists() else "MISSING")


def main() -> None:
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        return                                                  # another copy is already watching
    log("watchdog started")
    started: dict[str, float] = {}
    while True:
        for name, s in SERVERS.items():
            if up(s["port"]):
                continue
            if time.time() - started.get(name, 0) < GRACE:       # just started: give it time to come up
                continue
            try:
                start(name)
            except Exception as exc:  # noqa: BLE001 - try again on the next check
                log(f"could not start {name}: {exc}")
            started[name] = time.time()
        time.sleep(EVERY)


if __name__ == "__main__":
    check() if "--check" in sys.argv else main()
