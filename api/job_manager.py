import os
import sys
import time
import uuid
import json
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

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
                log_path=Path(meta["log_path"]),
                created_at=float(meta.get("created_at", time.time())),
                env=meta.get("env", {}),
            )

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

        proc = subprocess.Popen(
            [sys.executable, str(COORDINATOR_PATH)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_f,
            stderr=log_f,
        )

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
            },
        )

        self.jobs[jid] = job

        idx = _read_index()
        idx["jobs"][jid] = {
            "job_id": jid,
            "log_path": str(log_path),
            "created_at": job.created_at,
            "env": job.env,
        }
        _write_index(idx)

        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        return self.jobs.get(job_id)

    def status(self, job_id: str) -> dict:
        job = self.get_job(job_id)
        if not job:
            return {"job_id": job_id, "status": "NOT_FOUND"}

        if job.proc is not None:
            code = job.proc.poll()
            if code is None:
                return {"job_id": job_id, "status": "RUNNING", "pid": job.proc.pid}
            if code == 0:
                return {"job_id": job_id, "status": "COMPLETED", "exit_code": code}
            return {"job_id": job_id, "status": "FAILED", "exit_code": code}

        return {
            "job_id": job_id,
            "status": "PERSISTED",
            "note": "API restarted; process handle not available",
        }

    def list_jobs(self) -> list:
        return [self.status(jid) for jid in self.jobs.keys()]

    def tail_logs(self, job_id: str, tail: int = 200) -> str:
        job = self.get_job(job_id)
        if not job or not job.log_path.exists():
            return ""
        lines = job.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def read_new_log_bytes(self, job_id: str, offset: int) -> tuple[str, int]:
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

        if job.proc is None:
            return {
                "job_id": job_id,
                "status": "CANNOT_STOP",
                "note": "no live process handle (API restarted)",
            }

        code = job.proc.poll()
        if code is not None:
            return {"job_id": job_id, "status": "NOT_RUNNING", "exit_code": code}

        try:
            job.proc.send_signal(signal.SIGINT)
        except Exception:
            job.proc.terminate()

        return {"job_id": job_id, "status": "STOP_SIGNAL_SENT"}


job_manager = JobManager()
