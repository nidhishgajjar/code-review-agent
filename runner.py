#!/usr/bin/env python3
import os
import pathlib
import signal
import subprocess
import sys
import time

NUM_AGENTS = int(os.environ.get("NUM_AGENTS", "2"))
LOG_DIR = pathlib.Path(os.environ.get("LOG_DIR", "/agent/data/logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)


def spawn(agent_id: int) -> subprocess.Popen:
    env = {**os.environ, "AGENT_ID": str(agent_id), "NUM_AGENTS": str(NUM_AGENTS)}
    log_path = LOG_DIR / f"agent-{agent_id}.log"
    log = log_path.open("ab", buffering=0)
    return subprocess.Popen(
        [sys.executable, "-u", "agent.py"],
        env=env, stdout=log, stderr=subprocess.STDOUT,
    )


def main() -> int:
    procs = {i: spawn(i) for i in range(1, NUM_AGENTS + 1)}
    print(f"[runner] spawned {NUM_AGENTS} agents: {list(procs.keys())}", flush=True)

    def shutdown(*_):
        print("[runner] shutting down", flush=True)
        for p in procs.values():
            p.terminate()
        for p in procs.values():
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    while True:
        for aid, p in list(procs.items()):
            if p.poll() is not None:
                print(f"[runner] agent-{aid} exited code={p.returncode}, restarting in 5s", flush=True)
                time.sleep(5)
                procs[aid] = spawn(aid)
        time.sleep(3)


if __name__ == "__main__":
    sys.exit(main())
