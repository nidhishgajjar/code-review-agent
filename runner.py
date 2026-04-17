#!/usr/bin/env python3
"""
Thin supervisor: start agent.py (Flask webhook server).
If it crashes, tee logs and restart with backoff.
"""
import os
import pathlib
import signal
import subprocess
import sys
import time

LOG_DIR = pathlib.Path(os.environ.get("LOG_DIR", "/agent/data/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_PATH = LOG_DIR / "agent.log"


def spawn() -> subprocess.Popen:
    log = LOG_PATH.open("ab", buffering=0)
    return subprocess.Popen(
        [sys.executable, "-u", "agent.py"],
        stdout=log, stderr=subprocess.STDOUT,
    )


def main() -> int:
    print(f"[runner] tee -> {LOG_PATH}", flush=True)
    proc = spawn()

    def shutdown(*_):
        print("[runner] shutting down", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    backoff = 2
    while True:
        rc = proc.wait()
        print(f"[runner] agent exited code={rc}, restarting in {backoff}s", flush=True)
        time.sleep(backoff)
        backoff = min(backoff * 2, 60)
        proc = spawn()


if __name__ == "__main__":
    sys.exit(main())
