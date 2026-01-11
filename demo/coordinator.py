import os
import sys
import time
import signal
import subprocess
from pathlib import Path

# ---------------- CONFIG ----------------
JOB_ID = os.getenv("JOB_ID", "demo-job")
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "./checkpoints")
CHECKPOINT_EVERY = os.getenv("CHECKPOINT_EVERY", "5")
SLEEP_SEC = os.getenv("SLEEP_SEC", "0.5")

RANK = os.getenv("RANK", "0")
WORLD_SIZE = os.getenv("WORLD_SIZE", "1")

MAX_RESTARTS = int(os.getenv("MAX_RESTARTS", "50"))
RESTART_BACKOFF_SEC = float(os.getenv("RESTART_BACKOFF_SEC", "0.5"))
# ----------------------------------------


def project_root() -> Path:
    # demo/coordinator.py -> project root is parent of demo/
    return Path(__file__).resolve().parents[1]


def start_worker() -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "JOB_ID": JOB_ID,
            "RANK": str(RANK),
            "WORLD_SIZE": str(WORLD_SIZE),
            "CHECKPOINT_DIR": CHECKPOINT_DIR,
            "CHECKPOINT_EVERY": CHECKPOINT_EVERY,
            "SLEEP_SEC": SLEEP_SEC,
        }
    )

    worker_path = project_root() / "demo" / "worker.py"

    print(f"[coord] starting worker rank={RANK} job={JOB_ID}")
    print(f"[coord] worker script: {worker_path}")
    print(f"[coord] checkpoints:  {CHECKPOINT_DIR}")

    # Use same python interpreter that runs coordinator
    return subprocess.Popen(
        [sys.executable, str(worker_path)],
        env=env,
        stdout=sys.stdout,   # stream worker logs directly
        stderr=sys.stderr,
    )


def main():
    restarts = 0
    proc = start_worker()

    def shutdown(signum, frame):
        print(f"\n[coord] received signal {signum}; shutting down worker...")
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)  # graceful
                proc.wait(timeout=3)
            except Exception:
                proc.kill()
        print("[coord] done.")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        code = proc.poll()
        if code is None:
            time.sleep(0.2)
            continue

        print(f"\n[coord] worker exited with code={code}")

        # Restart policy (simple MVP)
        if restarts >= MAX_RESTARTS:
            print("[coord] max restarts hit; marking job FAILED (demo).")
            break

        restarts += 1
        print(f"[coord] restarting worker (attempt {restarts}/{MAX_RESTARTS})...")
        time.sleep(RESTART_BACKOFF_SEC)
        proc = start_worker()

    print("[coord] exiting.")


if __name__ == "__main__":
    main()
