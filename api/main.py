from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from api.job_manager import job_manager
import asyncio
from fastapi.responses import StreamingResponse
import asyncio
from sse_starlette.sse import EventSourceResponse


app = FastAPI(title="Distributed Training System API", version="0.1.0")


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
    return {"job_id": job.job_id, "pid": job.proc.pid}


@app.get("/jobs")
def list_jobs():
    return {"jobs": job_manager.list_jobs()}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    status = job_manager.status(job_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")
    return status


@app.get("/jobs/{job_id}/logs")
def get_job_logs(job_id: str, tail: int = Query(200, ge=1, le=5000)):
    status = job_manager.status(job_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")

    logs = job_manager.tail_logs(job_id, tail=tail)
    return {"job_id": job_id, "tail": tail, "logs": logs}

@app.post("/jobs/{job_id}/stop")
def stop_job(job_id: str):
    res = job_manager.stop_job(job_id)
    if res["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")
    return res


@app.get("/jobs/{job_id}/logs/stream")
async def stream_logs(job_id: str):
    status = job_manager.status(job_id)
    if status["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail="job not found")

    async def event_gen():
        offset = 0
        # stream until job ends and we've flushed logs
        while True:
            chunk, offset = job_manager.read_new_log_bytes(job_id, offset)
            if chunk:
                # SSE format
                yield f"data: {chunk.replace(chr(10), chr(10)+'data: ')}\n\n"

            st = job_manager.status(job_id)
            if st["status"] in ("COMPLETED", "FAILED"):
                # one last flush attempt
                chunk2, offset2 = job_manager.read_new_log_bytes(job_id, offset)
                if chunk2:
                    yield f"data: {chunk2.replace(chr(10), chr(10)+'data: ')}\n\n"
                break

            await asyncio.sleep(0.25)

    return StreamingResponse(event_gen(), media_type="text/event-stream")

