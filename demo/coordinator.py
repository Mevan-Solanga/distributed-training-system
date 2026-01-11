import os
import sys
import time
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

# ---------------- CONFIG ----------------
JOB_ID = os.getenv("JOB_ID", "demo-job")
CHECKPOINT_DIR = os.getenv("CHECKPOINT_DIR", "./checkpoints")
CHECKPOINT_EVERY = os.getenv("CHECKPOINT_EVERY", "5")
SLEEP_SEC = os.getenv("SLEEP_SEC", "0.5")

WORLD_SIZE = int(os.getenv("WORLD_SIZE", "4"))  # <<< number of workers

MAX_RESTARTS_PER_WORKER = int(os.getenv("MAX_RESTARTS_PER_WORKER", "50"))
RESTART_BACKOFF_SEC = float(os.getenv("RESTART_BACKOFF_SEC", "0.5"))
POLL_INTERVAL_SEC = float(os.getenv("POLL_INTERVAL_SEC", "0.2"))
DATASET_DIR = os.getenv("DATASET_DIR", "./data/shards")

# ----------------------------------------


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


@dataclass
class WorkerProc:
    rank: int
    proc: subprocess.Popen
    restarts: int = 0


def start_worker(rank: int) -> subprocess.Popen:
    env = os.environ.copy()
    env.update(
        {
            "JOB_ID": JOB_ID,
            "RANK": str(rank),
            "WORLD_SIZE": str(WORLD_SIZE),
            "CHECKPOINT_DIR": CHECKPOINT_DIR,
            "CHECKPOINT_EVERY": CHECKPOINT_EVERY,
            "SLEEP_SEC": SLEEP_SEC,
            "DATASET_DIR": DATASET_DIR,
        }
    )

    worker_path = project_root() / "demo" / "worker.py"
    print(f"[coord] starting worker rank={rank} (world_size={WORLD_SIZE}) pid=?")

    # # stdout/stderr inherit so you see all worker logs
    # return subprocess.Popen(
    #     [sys.executable, str(worker_path)],
    #     env=env,
    #     stdout=sys.stdout,
    #     stderr=sys.stderr,
    # )

    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW

    return subprocess.Popen(
        [sys.executable, str(worker_path)],
        env=env,
        stdout=sys.stdout,
        stderr=sys.stderr,
        creationflags=creationflags,
    )



def main():
    workers: Dict[int, WorkerProc] = {}

    print(f"[coord] job={JOB_ID}")
    print(f"[coord] checkpoints={CHECKPOINT_DIR}")
    print(f"[coord] world_size={WORLD_SIZE}")

    # start all workers
    for rank in range(WORLD_SIZE):
        proc = start_worker(rank)
        workers[rank] = WorkerProc(rank=rank, proc=proc)
        print(f"[coord] worker rank={rank} started pid={proc.pid}")

    shutting_down = False

    def shutdown(signum, frame):
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True

        print(f"\n[coord] received signal {signum}; stopping all workers...")
        for w in workers.values():
            if w.proc.poll() is None:
                try:
                    w.proc.send_signal(signal.SIGINT)
                except Exception:
                    pass

        # give them a moment to exit cleanly
        t0 = time.time()
        while time.time() - t0 < 3.0:
            if all(w.proc.poll() is not None for w in workers.values()):
                break
            time.sleep(0.1)

        # force kill remaining
        for w in workers.values():
            if w.proc.poll() is None:
                try:
                    w.proc.kill()
                except Exception:
                    pass

        print("[coord] done.")
        raise SystemExit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # monitor loop
    while True:
        all_exited = True

        for rank, w in list(workers.items()):
            code: Optional[int] = w.proc.poll()

            if code is None:
                all_exited = False
                continue

            # worker exited
            print(f"\n[coord] worker rank={rank} exited code={code}")

            # If worker finished normally, mark DONE and don't restart
            if code == 0:
                print(f"[coord] rank={rank} completed (exit 0). Marking DONE.")
                # Remove it from tracking so we don't restart it
                workers.pop(rank, None)
                continue

            if w.restarts >= MAX_RESTARTS_PER_WORKER:
                print(f"[coord] rank={rank} max restarts hit; leaving it dead (demo).")
                continue

            w.restarts += 1
            print(f"[coord] restarting rank={rank} (attempt {w.restarts}/{MAX_RESTARTS_PER_WORKER})...")
            time.sleep(RESTART_BACKOFF_SEC)
            new_proc = start_worker(rank)
            w.proc = new_proc
            print(f"[coord] worker rank={rank} restarted pid={new_proc.pid}")
            all_exited = False  # since we restarted it

        
        if len(workers) == 0:
            print("[coord] all workers DONE. Job COMPLETED.")
            break

        time.sleep(POLL_INTERVAL_SEC)




if __name__ == "__main__":
    main()
