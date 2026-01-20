import os
import json
import time
import shutil
import threading
from pathlib import Path
from itertools import islice

# Import S3 utils if available
try:
    from demo.s3_utils import use_s3, download_shard, list_shards, upload_checkpoint
    HAS_S3 = True
except ImportError:
    HAS_S3 = False

# Import training module
from demo.training import FakeModel, FakeOptimizer, train_step

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
HEARTBEAT_FILE = JOB_DIR / "HEARTBEAT"


def load_checkpoint():
    if not LATEST_FILE.exists():
        return {
            "step": 0,
            "rank": RANK,
            "world_size": WORLD_SIZE,
            "shard_idx": 0,
            "line_idx": 0,
            "model_state": None,
        }

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
    state.setdefault("model_state", None)

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

    # Write state
    with open(tmp_dir / "state.json", "w", encoding="utf-8") as f:
        json.dump(state, f)

    # Write manifest with metadata
    manifest = {
        "step": step,
        "timestamp": time.time(),
        "rank": state.get("rank", RANK),
        "world_size": state.get("world_size", WORLD_SIZE),
        "committed": True,
    }
    with open(tmp_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f)

    # Fsync for durability
    try:
        state_path = tmp_dir / "state.json"
        with open(state_path, "r") as f:
            os.fsync(f.fileno())
    except Exception:
        pass

    # Atomic rename
    try:
        tmp_dir.rename(final_dir)
    except FileExistsError:
        _safe_rmtree(tmp_dir)

    # Update LATEST pointer atomically
    LATEST_FILE.write_text(final_dir.name)


def assigned_shards():
    """Get list of shards assigned to this worker (round-robin by rank)."""
    if HAS_S3 and use_s3():
        # Use S3
        bucket = os.getenv("S3_BUCKET", "training-data")
        shards = list_shards(bucket, prefix="shards/")
        # Filter by rank
        assigned = [s for i, s in enumerate(shards) if i % WORLD_SIZE == RANK]
        return assigned
    else:
        # Use local filesystem
        all_shards = sorted(DATASET_DIR.glob("shard_*.txt"))

        def shard_index(path: Path) -> int:
            return int(path.stem.split("_")[1])

        return [p for p in all_shards if shard_index(p) % WORLD_SIZE == RANK]


def heartbeat_loop():
    """Write heartbeat file every 2 seconds so coordinator knows we're alive."""
    while True:
        try:
            HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT_FILE.write_text(json.dumps({"timestamp": time.time(), "rank": RANK, "pid": os.getpid()}))
        except Exception:
            pass
        time.sleep(2.0)


def main():
    JOB_DIR.mkdir(parents=True, exist_ok=True)

    # Start heartbeat thread
    hb_thread = threading.Thread(target=heartbeat_loop, daemon=True)
    hb_thread.start()

    state = load_checkpoint()
    print(f"[worker {RANK}] starting from step {state['step']}")

    shards = assigned_shards()
    use_s3_flag = HAS_S3 and use_s3()
    shard_names = [Path(s).name if isinstance(s, str) else s.name for s in shards]
    print(f"[worker {RANK}] assigned {len(shards)} shard(s) {'(from S3)' if use_s3_flag else '(local)'}: {shard_names}")

    if not shards:
        print(f"[worker {RANK}] no shards assigned. Exiting.")
        return
    
    # Initialize model and optimizer
    model = FakeModel(input_size=10, hidden_size=64, output_size=1)
    optimizer = FakeOptimizer(model, learning_rate=0.001)
    
    # Load model state from checkpoint if available
    if state.get("model_state"):
        print(f"[worker {RANK}] loading model state from checkpoint")
        model.load_state_dict(state["model_state"])
        print(f"[worker {RANK}] model loaded with {len(model.loss_history)} loss history entries")

    # Clamp resume values
    state["shard_idx"] = min(int(state.get("shard_idx", 0)), len(shards) - 1)
    state["line_idx"] = max(int(state.get("line_idx", 0)), 0)

    print(
        f"[worker {RANK}] resuming at shard_idx={state['shard_idx']} "
        f"line_idx={state['line_idx']} step={state['step']}"
    )

    for si in range(state["shard_idx"], len(shards)):
        shard = shards[si]
        
        # Download from S3 if needed
        if use_s3_flag:
            bucket = os.getenv("S3_BUCKET", "training-data")
            local_shard_path = DATASET_DIR / Path(shard).name
            if not local_shard_path.exists():
                print(f"[worker {RANK}] downloading shard {shard}...")
                if not download_shard(bucket, shard, local_shard_path):
                    print(f"[worker {RANK}] failed to download {shard}")
                    continue
            shard_path = local_shard_path
        else:
            shard_path = shard

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
                
                # Execute ML training step
                loss = train_step(model, optimizer, batch_size=32)
                
                print(
                    f"[worker {RANK}] step {state['step']} | loss {loss:.4f} | {sample} "
                    f"| (si={state['shard_idx']} li={state['line_idx']})"
                )

                if state["step"] % CHECKPOINT_EVERY == 0:
                    # Save model state to checkpoint
                    state["model_state"] = model.state_dict()
                    print(f"[worker {RANK}] checkpointing at step {state['step']} (loss: {loss:.4f})")
                    save_checkpoint(state)

    print(f"[worker {RANK}] finished all assigned shards. Exiting.")



if __name__ == "__main__":
    main()
