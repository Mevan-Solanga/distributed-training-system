import os
import json
import time
import shutil
from pathlib import Path
from itertools import islice

# ---------- CONFIG ----------
JOB_ID = os.getenv("JOB_ID", "demo-job")
RANK = int(os.getenv("RANK", "0"))
WORLD_SIZE = int(os.getenv("WORLD_SIZE", "1"))

CHECKPOINT_DIR = Path(os.getenv("CHECKPOINT_DIR", "./checkpoints"))
CHECKPOINT_EVERY = int(os.getenv("CHECKPOINT_EVERY", "5"))
SLEEP_SEC = float(os.getenv("SLEEP_SEC", "0.5"))

DATASET_DIR = Path(os.getenv("DATASET_DIR", "./data/shards"))
# ----------------------------

JOB_DIR = CHECKPOINT_DIR / JOB_ID / f"worker_{RANK}"
LATEST_FILE = JOB_DIR / "LATEST"


def load_checkpoint():
    if not LATEST_FILE.exists():
        return {"step": 0, "rank": RANK, "world_size": WORLD_SIZE, "shard_idx": 0, "line_idx": 0}

    step_dir = LATEST_FILE.read_text().strip()
    state_path = JOB_DIR / step_dir / "state.json"

    if not state_path.exists():
        raise RuntimeError("LATEST points to missing checkpoint")

    with open(state_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    # Backward-compatible defaults
    state.setdefault("step", 0)
    state.setdefault("rank", RANK)
    state.setdefault("world_size", WORLD_SIZE)
    state.setdefault("shard_idx", 0)
    state.setdefault("line_idx", 0)

    return state


def _safe_rmtree(p: Path):
    try:
        if p.exists():
            shutil.rmtree(p)
    except Exception:
        pass


def save_checkpoint(state):
    step = state["step"]
    tmp_dir = JOB_DIR / f"step_{step}_tmp_{int(time.time()*1000)}_{os.getpid()}"
    final_dir = JOB_DIR / f"step_{step}"

    # If this checkpoint already exists, just update LATEST
    if final_dir.exists():
        LATEST_FILE.write_text(final_dir.name)
        return

    _safe_rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)

    with open(tmp_dir / "state.json", "w", encoding="utf-8") as f:
        json.dump(state, f)

    try:
        tmp_dir.rename(final_dir)
    except FileExistsError:
        _safe_rmtree(tmp_dir)

    LATEST_FILE.write_text(final_dir.name)


def assigned_shards():
    all_shards = sorted(DATASET_DIR.glob("shard_*.txt"))

    def shard_index(path: Path) -> int:
        return int(path.stem.split("_")[1])

    return [p for p in all_shards if shard_index(p) % WORLD_SIZE == RANK]


def main():
    JOB_DIR.mkdir(parents=True, exist_ok=True)

    state = load_checkpoint()
    print(f"[worker {RANK}] starting from step {state['step']}")

    shards = assigned_shards()
    print(f"[worker {RANK}] assigned {len(shards)} shard(s): {[p.name for p in shards]}")

    if not shards:
        print(f"[worker {RANK}] no shards assigned. Exiting.")
        return

    # Clamp resume values
    state["shard_idx"] = min(int(state.get("shard_idx", 0)), len(shards) - 1)
    state["line_idx"] = max(int(state.get("line_idx", 0)), 0)

    print(
        f"[worker {RANK}] resuming at shard_idx={state['shard_idx']} "
        f"line_idx={state['line_idx']} step={state['step']}"
    )

    for si in range(state["shard_idx"], len(shards)):
        shard_path = shards[si]

        if si != state["shard_idx"]:
            state["line_idx"] = 0

        start_line = int(state["line_idx"])

        with shard_path.open("r", encoding="utf-8") as f:
            for line in islice(f, start_line, None):
                time.sleep(SLEEP_SEC)

                state["step"] += 1
                state["shard_idx"] = si
                state["line_idx"] += 1

                sample = line.strip()
                print(
                    f"[worker {RANK}] step {state['step']} | {sample} "
                    f"| (si={state['shard_idx']} li={state['line_idx']})"
                )

                if state["step"] % CHECKPOINT_EVERY == 0:
                    print(f"[worker {RANK}] checkpointing at step {state['step']}")
                    save_checkpoint(state)

    print(f"[worker {RANK}] finished all assigned shards. Exiting.")


if __name__ == "__main__":
    main()
