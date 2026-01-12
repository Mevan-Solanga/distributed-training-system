import os
import sys
import time
import uuid
import json
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple
import psutil

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COORDINATOR_PATH = PROJECT_ROOT / "demo" / "coordinator.py"
INDEX_PATH = LOG_DIR / "index.json"


def _read_index() -> dict:
    if not INDEX_PATH.exists():
        return {"jobs": {}}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"jobs": {}}


def _write_index(data: dict) -> None:
    INDEX_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_index_job(idx: dict, job_id: str) -> dict:
    if "jobs" not in idx:
        idx["jobs"] = {}
    if job_id not in idx["jobs"]:
        idx["jobs"][job_id] = {"job_id": job_id}
    return idx


@dataclass
class Job:
    job_id: str
    proc: Optional[subprocess.Popen]  # None after API restart
    pid: Optional[int]
    log_path: Path
    created_at: float
    env: dict


class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self._load_from_disk()

    def _load_from_disk(self):
        idx = _read_index()
        for jid, meta in idx.get("jobs", {}).items():
            self.jobs[jid] = Job(
                job_id=jid,
                proc=None,
                pid=meta.get("pid"),
                log_path=Path(meta["log_path"]),
                created_at=float(meta.get("created_at", time.time())),
                env=meta.get("env", {}),
            )

    def _patch_index(self, job_id: str, patch: dict) -> None:
        idx = _read_index()
        _ensure_index_job(idx, job_id)
        idx["jobs"][job_id].update(patch)
        _write_index(idx)

    def _pid_alive(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False

    def _log_indicates_completed(self, job: Job) -> bool:
        if not job.log_path.exists():
            return False
        try:
            # Read last chunk only (fast)
            data = job.log_path.read_text(encoding="utf-8", errors="replace")
            return "Job COMPLETED." in data or "all workers DONE. Job COMPLETED." in data
        except Exception:
            return False

    def create_job(
        self,
        world_size: int = 4,
        checkpoint_every: int = 5,
        sleep_sec: float = 0.5,
        dataset_dir: str = "./data/shards",
        checkpoint_dir: str = "./checkpoints",
        job_id: Optional[str] = None,
    ) -> Job:
        jid = job_id or f"job-{uuid.uuid4().hex[:8]}"
        log_path = LOG_DIR / f"{jid}.log"

        env = os.environ.copy()
        env.update(
            {
                "JOB_ID": jid,
                "WORLD_SIZE": str(world_size),
                "CHECKPOINT_DIR": checkpoint_dir,
                "CHECKPOINT_EVERY": str(checkpoint_every),
                "SLEEP_SEC": str(sleep_sec),
                "DATASET_DIR": dataset_dir,
            }
        )

        log_f = open(log_path, "a", encoding="utf-8", buffering=1)

        creationflags = 0
        # IMPORTANT: detach on Windows so stopping Uvicorn does NOT send Ctrl+C to the job
        if os.name == "nt":
            creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

        proc = subprocess.Popen(
            [sys.executable, str(COORDINATOR_PATH)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_f,
            stderr=log_f,
            creationflags=creationflags,
        )

        job = Job(
            job_id=jid,
            proc=proc,
            pid=proc.pid,
            log_path=log_path,
            created_at=time.time(),
            env={
                "WORLD_SIZE": str(world_size),
                "CHECKPOINT_EVERY": str(checkpoint_every),
                "SLEEP_SEC": str(sleep_sec),
                "DATASET_DIR": dataset_dir,
                "CHECKPOINT_DIR": checkpoint_dir,
            },
        )

        self.jobs[jid] = job

        self._patch_index(
            jid,
            {
                "job_id": jid,
                "pid": proc.pid,
                "log_path": str(log_path),
                "created_at": job.created_at,
                "env": job.env,
                "status": "RUNNING",
                "exit_code": None,
                "ended_at": None,
            },
        )

        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def status(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "NOT_FOUND"}

        # If we still have a live Popen handle (same API process)
        if job.proc is not None:
            code = job.proc.poll()
            if code is None:
                return {"job_id": job_id, "status": "RUNNING", "pid": job.proc.pid}
            # Process ended; persist final status once
            final = "COMPLETED" if code == 0 else "FAILED"
            self._patch_index(
                job_id,
                {"status": final, "exit_code": int(code), "ended_at": time.time()},
            )
            return {"job_id": job_id, "status": final, "exit_code": int(code)}

        # After API restart: no proc handle, rely on persisted PID + log
        pid = job.pid
        if pid is not None and self._pid_alive(int(pid)):
            return {"job_id": job_id, "status": "RUNNING", "pid": int(pid), "note": "reattached via PID"}

        # PID not alive → job ended sometime; infer from log and persist
        if self._log_indicates_completed(job):
            self._patch_index(job_id, {"status": "COMPLETED", "exit_code": 0, "ended_at": time.time()})
            return {"job_id": job_id, "status": "COMPLETED", "exit_code": 0, "note": "inferred from logs"}

        # Otherwise unknown end (killed/crashed)
        # Keep as FAILED with unknown exit_code for MVP
        self._patch_index(job_id, {"status": "FAILED", "exit_code": None, "ended_at": time.time()})
        return {"job_id": job_id, "status": "FAILED", "note": "PID not running; completion not found in logs"}

    def list_jobs(self) -> list:
        return [self.status(jid) for jid in self.jobs.keys()]

    def tail_logs(self, job_id: str, tail: int = 200) -> str:
        job = self.get_job(job_id)
        if not job or not job.log_path.exists():
            return ""
        lines = job.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def read_new_log_bytes(self, job_id: str, offset: int) -> Tuple[str, int]:
        job = self.get_job(job_id)
        if not job or not job.log_path.exists():
            return "", offset

        data = job.log_path.read_bytes()
        if offset >= len(data):
            return "", offset

        new = data[offset:]
        text = new.decode("utf-8", errors="replace")
        return text, len(data)

    def stop_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "NOT_FOUND"}

        pid = job.pid if job.pid is not None else None
        if pid is None:
            return {"job_id": job_id, "status": "CANNOT_STOP", "note": "no PID known"}

        pid = int(pid)
        if not self._pid_alive(pid):
            return {"job_id": job_id, "status": "NOT_RUNNING", "note": "PID not alive"}

        # Detached jobs can't reliably receive Ctrl+C; terminate for MVP
        try:
            psutil.Process(pid).terminate()
            return {"job_id": job_id, "status": "STOP_SENT", "note": "terminated via PID"}
        except Exception as e:
            return {"job_id": job_id, "status": "STOP_FAILED", "error": str(e)}

    def delete_job(self, job_id: str, delete_logs: bool = False) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "NOT_FOUND"}

        # If it's still running, stop it first (best-effort)
        st = self.status(job_id)
        if st.get("status") == "RUNNING":
            self.stop_job(job_id)

        # Remove from memory
        self.jobs.pop(job_id, None)

        # Remove from index.json
        idx = _read_index()
        if "jobs" in idx and job_id in idx["jobs"]:
            idx["jobs"].pop(job_id, None)
            _write_index(idx)

        # Optionally delete the log file
        if delete_logs and job.log_path.exists():
            try:
                job.log_path.unlink()
            except Exception:
                pass

        return {"job_id": job_id, "status": "DELETED", "deleted_logs": bool(delete_logs)}

    def cleanup_jobs(self, keep_last: int = 50, delete_logs: bool = False) -> dict:
        """
        Keep only N most recent jobs in index + memory.
        (Useful because index.json grows forever.)
        """
        idx = _read_index()
        jobs_map = idx.get("jobs", {})

        # Sort by created_at (desc). Missing created_at -> treat as 0.
        items = sorted(
            jobs_map.items(),
            key=lambda kv: float(kv[1].get("created_at", 0.0)),
            reverse=True,
        )

        keep = dict(items[: max(0, int(keep_last))])
        remove = dict(items[max(0, int(keep_last)) :])

        removed_ids = list(remove.keys())

        # Delete logs for removed jobs if requested
        if delete_logs:
            for jid, meta in remove.items():
                lp = meta.get("log_path")
                if lp:
                    try:
                        Path(lp).unlink(missing_ok=True)  # py3.8+: missing_ok supported
                    except TypeError:
                        # fallback for older
                        p = Path(lp)
                        if p.exists():
                            try:
                                p.unlink()
                            except Exception:
                                pass
                    except Exception:
                        pass

        # Write trimmed index
        idx["jobs"] = keep
        _write_index(idx)

        # Also trim in-memory dict (best-effort)
        for jid in removed_ids:
            self.jobs.pop(jid, None)

        return {
            "status": "OK",
            "kept": len(keep),
            "removed": len(remove),
            "removed_job_ids": removed_ids,
            "deleted_logs": bool(delete_logs),
        }



job_manager = JobManager()
