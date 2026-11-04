"""API Endpoints cho Artifact Store trên PC1.

Endpoints:
- POST /api/v1/artifacts: tải lên tệp artifact kèm metadata (multipart form-data)
- GET /api/v1/artifacts/{id}/content: tải nội dung tệp artifact kèm header X-Checksum-SHA256
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from agent_contract.models import ArtifactRef
from control.app.api.dependencies import get_db, require_authenticated
from control.app.security.authentication import Principal, WorkerPrincipal
from control.app.services.artifact_store import (
    ArtifactStoreError,
    ArtifactStoreService,
    model_to_artifact_ref,
)

router = APIRouter(prefix="/artifacts", tags=["Artifacts"])
artifact_service = ArtifactStoreService()


@router.post("", status_code=201, response_model=ArtifactRef)
async def upload_artifact(
    file: UploadFile = File(..., description="Tệp nội dung artifact"),
    metadata: Optional[str] = Form(None, description="Chuỗi JSON metadata của ArtifactRef"),
    artifact_id: Optional[str] = Form(None),
    name: Optional[str] = Form(None),
    type: Optional[str] = Form(None),
    run_id: Optional[str] = Form(None),
    task_id: Optional[str] = Form(None),
    version: Optional[str] = Form("1.0"),
    checksum: Optional[str] = Form(None),
    created_by: Optional[str] = Form(None),
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> ArtifactRef:
    """Tải lên một Artifact kèm metadata (Multipart form-data)."""
    # 1. Trích xuất metadata từ chuỗi JSON hoặc các form fields
    meta_dict: Dict[str, Any] = {}
    if metadata:
        try:
            meta_dict = json.loads(metadata)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Metadata JSON không hợp lệ: {e}")

    final_art_id = artifact_id or meta_dict.get("artifact_id")
    final_name = name or meta_dict.get("name") or file.filename or "artifact.dat"
    final_type = type or meta_dict.get("type") or "OTHER"
    final_run_id = run_id or meta_dict.get("run_id")
    final_task_id = task_id or meta_dict.get("task_id")
    final_version = version or meta_dict.get("version") or "1.0"
    final_checksum = checksum or meta_dict.get("checksum")

    # Người tạo (phải khớp định dạng agent_id: ^[a-z_]+-\d{2}$)
    default_creator = "devops-01"
    if isinstance(principal, WorkerPrincipal):
        default_creator = principal.allowed_agents[0] if principal.allowed_agents else "devops-01"
    final_created_by = created_by or meta_dict.get("created_by") or default_creator
    extra_meta = meta_dict.get("metadata", {})

    # 2. Đọc nội dung tệp
    try:
        content = await file.read()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Lỗi đọc nội dung file: {e}")

    # 3. Lưu trữ qua ArtifactStoreService
    try:
        model = artifact_service.save_artifact(
            session=session,
            content=content,
            filename=final_name,
            run_id=final_run_id or "global",
            task_id=final_task_id,
            artifact_type=final_type,
            created_by=final_created_by,
            artifact_id=final_art_id,
            version=final_version,
            expected_checksum=final_checksum,
            metadata=extra_meta,
        )
        session.commit()
        return model_to_artifact_ref(model)
    except ArtifactStoreError as e:
        session.rollback()
        raise HTTPException(status_code=e.status_code, detail=e.message)
    except Exception as e:
        session.rollback()
        raise HTTPException(status_code=500, detail=f"Lỗi hệ thống khi lưu artifact: {e}")


@router.get("/{artifact_id}/content")
def get_artifact_content(
    artifact_id: str,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> FileResponse:
    """Tải nội dung tệp artifact, kèm header X-Checksum-SHA256."""
    try:
        file_path, model = artifact_service.get_artifact_file_path(session, artifact_id)
        headers = {
            "X-Checksum-SHA256": model.checksum or "",
            "Content-Disposition": f'attachment; filename="{model.name}"',
        }
        return FileResponse(
            path=file_path,
            filename=model.name,
            headers=headers,
            media_type="application/octet-stream",
        )
    except ArtifactStoreError as e:
        raise HTTPException(status_code=e.status_code, detail=e.message)
