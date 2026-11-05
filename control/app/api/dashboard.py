"""Authenticated realtime snapshots and downloads for the local dashboard."""

import asyncio
from hashlib import file_digest
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from control.app.api.dependencies import get_db, require_admin
from control.app.database.models import ArtifactModel
from control.app.database.session import get_session_factory
from control.app.services.dashboard import dashboard_snapshot, physical_path


router = APIRouter(prefix="/dashboard", tags=["Realtime dashboard"])


def dashboard_session_factory():
    return get_session_factory()


@router.get("/snapshot")
def snapshot(run_id: str | None = None, session: Session = Depends(get_db), admin=Depends(require_admin)):
    try:
        return dashboard_snapshot(session, run_id)
    except LookupError as exc:
        raise HTTPException(404, detail="Run not found") from exc


async def snapshot_stream(request, factory, run_id=None, interval=1.0):
    revision = None
    while not await request.is_disconnected():
        try:
            def read():
                with factory() as session:
                    return dashboard_snapshot(session, run_id)
            data = await asyncio.to_thread(read)
            if data["revision"] != revision:
                revision = data["revision"]
                yield f"id: {revision}\nevent: snapshot\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            else:
                yield ": heartbeat\n\n"
        except LookupError:
            yield 'event: unavailable\ndata: {"message":"Run not found"}\n\n'
            return
        except Exception:
            revision = None
            # Keep the stream alive across a database outage without exposing connection credentials.
            yield 'event: unavailable\ndata: {"message":"Dữ liệu đang gián đoạn. Hệ thống đang thử kết nối lại."}\n\n'
        await asyncio.sleep(interval)


@router.get("/stream")
async def stream(request: Request, run_id: str | None = None,
                 factory=Depends(dashboard_session_factory), admin=Depends(require_admin)):
    return StreamingResponse(snapshot_stream(request, factory, run_id), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


@router.get("/artifacts/{artifact_id}/content")
def download(artifact_id: str, session: Session = Depends(get_db), admin=Depends(require_admin)):
    artifact = session.get(ArtifactModel, artifact_id)
    path = physical_path(artifact) if artifact else None
    if not path or not path.is_file():
        raise HTTPException(404, detail="Artifact file not found on the Control Node")
    if artifact.checksum:
        with path.open("rb") as handle:
            if file_digest(handle, "sha256").hexdigest() != artifact.checksum:
                raise HTTPException(409, detail="Artifact checksum mismatch")
    return FileResponse(path, filename=artifact.name, headers={"X-Checksum-SHA256": artifact.checksum or ""})
