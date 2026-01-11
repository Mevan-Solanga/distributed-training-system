import os
import json
import time
from pathlib import Path

# ---------- CONFIG ----------
JOB_ID = os.getenv("JOB_ID", "demo-job")
RANK = int(os.getenv("RANK", "0"))
CHECKPOINT_DIR = Path(os.getenv("CHECKPOINT_DIR", "./checkpoints"))
CHECKPOINT_EVERY = int(os.getenv("CHECKPOINT_EVERY", "5"))
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.5"))
# ----------------------------

JOB_DIR = CHECKPOINT_DIR / JOB_ID / f"worker_{RANK}"
LATEST_FILE = JOB_DIR / "LATEST"


def load_checkpoint():
    if not LATEST_FILE.exists():
        return {"step": 0}

    step_dir = LATEST_FILE.read_text().strip()
    state_path = JOB_DIR / step_dir / "state.json"

    if not state_path.exists():
        raise RuntimeError("LATEST points to missing checkpoint")

    with open(state_path) as f:
        return json.load(f)


def save_checkpoint(state):
    step = state["step"]
    tmp_dir = JOB_DIR / f"step_{step}_tmp"
    final_dir = JOB_DIR / f"step_{step}"

    tmp_dir.mkdir(parents=True, exist_ok=True)

    with open(tmp_dir / "state.json", "w") as f:
        json.dump(state, f)

    # atomic rename
    tmp_dir.rename(final_dir)

    # update LATEST last
    LATEST_FILE.write_text(final_dir.name)


def main():
    JOB_DIR.mkdir(parents=True, exist_ok=True)

    state = load_checkpoint()
    print(f"[worker {RANK}] starting from step {state['step']}")

    while True:
        time.sleep(SLEEP_SEC)
        state["step"] += 1
        print(f"[worker {RANK}] step {state['step']}")

        if state["step"] % CHECKPOINT_EVERY == 0:
            print(f"[worker {RANK}] checkpointing at step {state['step']}")
            save_checkpoint(state)


if __name__ == "__main__":
    main()
