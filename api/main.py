from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from api.job_manager import job_manager
import asyncio
from fastapi.responses import StreamingResponse

app = FastAPI(title="Distributed Training System API", version="0.2.0")


class CreateJobRequest(BaseModel):
    world_size: int = Field(4, ge=1, le=64)
    checkpoint_every: int = Field(5, ge=1, le=10_000)
    sleep_sec: float = Field(0.5, ge=0.0, le=10.0)
    dataset_dir: str = "./data/shards"
    checkpoint_dir: str = "./checkpoints"
    job_id: str | None = None


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/jobs")
def create_job(req: CreateJobRequest):
    job = job_manager.create_job(
        world_size=req.world_size,
        checkpoint_every=req.checkpoint_every,
        sleep_sec=req.sleep_sec,
        dataset_dir=req.dataset_dir,
        checkpoint_dir=req.checkpoint_dir,
        job_id=req.job_id,
    )
    return {"job_id": job.job_id, "pid": job.pid}


@app.get("/jobs")
def list_jobs():
    return {"jobs": job_manager.list_jobs()}


@app.get("/jobs/summaries")
def list_job_summaries():
    return {"jobs": job_manager.list_summaries()}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    status = job_manager.status(job_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")
    return status


@app.get("/jobs/{job_id}/summary")
def get_job_summary(job_id: str):
    summary = job_manager.summary(job_id)
    if summary["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")
    return summary


@app.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str, tail: int = Query(200, ge=1, le=5000)):
    status = job_manager.status(job_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")

    logs = job_manager.tail_logs(job_id, tail=tail)
    return {"job_id": job_id, "tail": tail, "logs": logs}


@app.get("/jobs/{job_id}/logs/stream")
async def stream_logs(job_id: str):
    status = job_manager.status(job_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")

    async def event_gen():
        offset = 0
        while True:
            chunk, offset = job_manager.read_new_log_bytes(job_id, offset)
            if chunk:
                yield f"data: {chunk.replace(chr(10), chr(10)+'data: ')}\n\n"

            st = job_manager.status(job_id)
            if st["status"] in ("COMPLETED", "FAILED", "LOST"):
                # final flush
                chunk2, offset2 = job_manager.read_new_log_bytes(job_id, offset)
                if chunk2:
                    yield f"data: {chunk2.replace(chr(10), chr(10)+'data: ')}\n\n"
                break

            await asyncio.sleep(0.25)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    res = job_manager.stop_job(job_id)
    if res["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")
    return res


@app.delete("/jobs/{job_id}")
def delete_job(
    job_id: str,
    delete_logs: bool = Query(True),
    stop_first: bool = Query(False),
    force: bool = Query(False),
):
    res = job_manager.delete_job(
        job_id,
        delete_logs=delete_logs,
        stop_first=stop_first,
        force=force,
    )
    if res["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")
    if res["status"] == "REFUSED_RUNNING":
        raise HTTPException(status_code=409, detail=res["note"])
    return res


@app.post("/jobs/purge")
def purge_jobs(
    older_than_hours: float | None = Query(None, ge=0.0),
    older_than_days: float | None = Query(None, ge=0.0),
    statuses: list[str] = Query(default=[]),
    delete_logs: bool = Query(True),
    stop_running: bool = Query(False),
    force: bool = Query(False),
):
    # Convert to seconds if provided
    older_than_seconds = None
    if older_than_days is not None:
        older_than_seconds = float(older_than_days) * 24.0 * 3600.0
    elif older_than_hours is not None:
        older_than_seconds = float(older_than_hours) * 3600.0

    res = job_manager.purge(
        older_than_seconds=older_than_seconds,
        statuses=statuses if statuses else None,
        delete_logs=delete_logs,
        stop_running=stop_running,
        force=force,
    )
    return res
