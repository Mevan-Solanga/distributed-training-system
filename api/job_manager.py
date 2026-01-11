# api/job_manager.py
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


@dataclass
class Job:
    job_id: str
    proc: Optional[subprocess.Popen]  # None after API restart
    log_path: Path
    created_at: float
    env: dict  # config + PID maybe


class JobManager:
    def __init__(self):
        self.jobs: Dict[str, Job] = {}
        self._load_from_disk()

    # ---------------- Index helpers ----------------
    def _update_index_job(self, job_id: str, patch: dict) -> None:
        idx = _read_index()
        idx.setdefault("jobs", {})
        idx["jobs"].setdefault(job_id, {})
        idx["jobs"][job_id].update(patch)
        _write_index(idx)

    # ---------------- Restore ----------------
    def _load_from_disk(self) -> None:
        idx = _read_index()
        for jid, meta in idx.get("jobs", {}).items():
            env = dict(meta.get("env", {}))
            pid = meta.get("pid")
            if pid is not None:
                env["PID"] = int(pid)

            # restore persisted terminal status if present
            if meta.get("exit_code") is not None:
                env["EXIT_CODE"] = int(meta["exit_code"])

            self.jobs[jid] = Job(
                job_id=jid,
                proc=None,
                log_path=Path(meta["log_path"]),
                created_at=float(meta.get("created_at", time.time())),
                env=env,
            )

    # ---------------- Create ----------------
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

        popen_kwargs = dict(
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_f,
            stderr=log_f,
        )

        # Detach so uvicorn restart doesn't kill the job
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen([sys.executable, str(COORDINATOR_PATH)], **popen_kwargs)

        job = Job(
            job_id=jid,
            proc=proc,
            log_path=log_path,
            created_at=time.time(),
            env={
                "WORLD_SIZE": str(world_size),
                "CHECKPOINT_EVERY": str(checkpoint_every),
                "SLEEP_SEC": str(sleep_sec),
                "DATASET_DIR": dataset_dir,
                "CHECKPOINT_DIR": checkpoint_dir,
                "PID": int(proc.pid),
            },
        )
        self.jobs[jid] = job

        idx = _read_index()
        idx.setdefault("jobs", {})
        idx["jobs"][jid] = {
            "job_id": jid,
            "pid": proc.pid,
            "log_path": str(log_path),
            "created_at": job.created_at,
            "env": {
                "WORLD_SIZE": str(world_size),
                "CHECKPOINT_EVERY": str(checkpoint_every),
                "SLEEP_SEC": str(sleep_sec),
                "DATASET_DIR": dataset_dir,
                "CHECKPOINT_DIR": checkpoint_dir,
            },
        }
        _write_index(idx)

        return job

    # ---------------- Basic getters ----------------
    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def list_jobs(self) -> list:
        return [self.status(jid) for jid in self.jobs.keys()]

    # ---------------- Logs ----------------
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

    # ---------------- Status ----------------
    def status(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "NOT_FOUND"}

        # If we have a live Popen handle (no restart)
        if job.proc is not None:
            code = job.proc.poll()
            if code is None:
                return {"job_id": job_id, "status": "RUNNING", "pid": job.proc.pid}

            # terminal: persist
            self._update_index_job(job_id, {"exit_code": int(code), "finished_at": time.time()})
            if code == 0:
                return {"job_id": job_id, "status": "COMPLETED", "exit_code": 0}
            return {"job_id": job_id, "status": "FAILED", "exit_code": int(code)}

        # API restarted path: consult index first
        idx = _read_index()
        meta = idx.get("jobs", {}).get(job_id, {})

        if meta.get("exit_code") is not None:
            exit_code = int(meta["exit_code"])
            if exit_code == 0:
                return {"job_id": job_id, "status": "COMPLETED", "exit_code": 0, "note": "persisted"}
            return {"job_id": job_id, "status": "FAILED", "exit_code": exit_code, "note": "persisted"}

        # If PID exists and alive -> RUNNING
        pid = meta.get("pid", job.env.get("PID"))
        if pid is not None and self._pid_alive(int(pid)):
            return {"job_id": job_id, "status": "RUNNING", "pid": int(pid), "note": "reattached via PID"}

        # PID is dead. Determine terminal status from log.
        inferred = self._infer_terminal_from_log(job.log_path)
        if inferred is not None:
            exit_code, reason = inferred
            self._update_index_job(job_id, {"exit_code": int(exit_code), "finished_at": time.time(), "exit_reason": reason})
            if exit_code == 0:
                return {"job_id": job_id, "status": "COMPLETED", "exit_code": 0, "note": f"inferred from log: {reason}"}
            return {"job_id": job_id, "status": "FAILED", "exit_code": int(exit_code), "note": f"inferred from log: {reason}"}

        return {"job_id": job_id, "status": "LOST", "note": "API restarted; PID not running and could not infer from log"}

    def _infer_terminal_from_log(self, log_path: Path) -> Optional[Tuple[int, str]]:
        if not log_path.exists():
            return None

        # Read only the tail for speed
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
            tail = "\n".join(lines[-300:])
        except Exception:
            return None

        # Your coordinator prints this exact line
        if "Job COMPLETED." in tail or "all workers DONE. Job COMPLETED." in tail:
            return 0, "completed_marker"

        # Some basic failure markers (optional / best-effort)
        if "max restarts hit" in tail.lower():
            return 1, "max_restarts_hit"
        if "Traceback (most recent call last)" in tail:
            return 1, "python_traceback"

        return None

    # ---------------- Stop ----------------
    def stop_job(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "NOT_FOUND"}

        # live handle
        if job.proc is not None:
            code = job.proc.poll()
            if code is not None:
                self._update_index_job(job_id, {"exit_code": int(code), "finished_at": time.time()})
                return {"job_id": job_id, "status": "NOT_RUNNING", "exit_code": int(code)}

            try:
                if os.name == "nt":
                    job.proc.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    job.proc.send_signal(signal.SIGINT)
            except Exception:
                job.proc.terminate()

            return {"job_id": job_id, "status": "STOP_SIGNAL_SENT"}

        # restarted: PID kill
        idx = _read_index()
        meta = idx.get("jobs", {}).get(job_id, {})
        pid = meta.get("pid", job.env.get("PID"))
        if pid is None:
            return {"job_id": job_id, "status": "CANNOT_STOP", "note": "no proc handle and no PID"}

        pid = int(pid)
        if not self._pid_alive(pid):
            return {"job_id": job_id, "status": "NOT_RUNNING", "note": "PID not alive"}

        try:
            p = psutil.Process(pid)
            if os.name == "nt":
                p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                p.send_signal(signal.SIGINT)
            return {"job_id": job_id, "status": "STOP_SIGNAL_SENT", "note": "stopped via PID"}
        except Exception as e:
            return {"job_id": job_id, "status": "STOP_FAILED", "error": str(e)}

    # ---------------- PID helper ----------------
    def _pid_alive(self, pid: int) -> bool:
        try:
            p = psutil.Process(pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False


job_manager = JobManager()
