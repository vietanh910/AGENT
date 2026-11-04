"""Endpoint quản lý Workflow Runs và phê duyệt Gates trên PC1."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode
from agent_contract.models import AgentError, ArtifactRef
from agent_contract.system_models import DispatchState, RunStatus, SystemEventType
from control.app.api.dependencies import get_db, get_publisher, require_admin, require_authenticated
from control.app.config import settings
from control.app.database.models import ApprovalModel, TaskModel, WorkflowRunModel
from control.app.database.repositories import ApprovalRepository, AuditRepository, TaskRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.orchestrator.engine import OrchestratorEngine
from control.app.queue.events import EventPublisher, get_event_publisher
from control.app.security.authentication import AdminPrincipal, Principal
from control.app.services.approval import ApprovalService
from control.app.services.artifact_store import ArtifactStoreService, model_to_artifact_ref
from control.app.services.plan_service import PlanService

router = APIRouter(prefix="/runs", tags=["Runs"])
artifact_service = ArtifactStoreService()


class CreateRunRequest(BaseModel):
    run_id: Optional[str] = None
    project_id: Optional[str] = Field(None, description="ID dự án")
    prompt: Optional[str] = Field(None, description="Yêu cầu từ người dùng")
    brief: Optional[str] = Field(None, description="Mô tả tóm tắt yêu cầu phần mềm")
    plan: Optional[Dict[str, Any]] = None


class ApproveGateRequest(BaseModel):
    gate: Optional[str] = Field(None, description="Tên cổng kiểm duyệt (ví dụ: APPROVE_SCOPE, APPROVE_PROD, APPROVE_PLAN)")
    notes: Optional[str] = None


class RejectGateRequest(BaseModel):
    gate: str
    reason: str


class UnblockTaskRequest(BaseModel):
    additional_context: Dict[str, Any] = Field(default_factory=dict)
    comment: Optional[str] = None


def _background_plan_task(run_id: str, prompt: str, project_id: str) -> None:
    """Hàm chạy nền thực thi lập kế hoạch dự án bằng PlanService."""
    factory = get_session_factory()
    with factory() as session:
        try:
            pub = get_event_publisher()
            service = PlanService(publisher=pub)
            service.create_project_plan(
                session=session,
                run_id=run_id,
                prompt=prompt,
                project_id=project_id,
            )
        except Exception as exc:
            # Ghi nhận lỗi nếu lập kế hoạch thất bại
            import logging
            logging.getLogger(__name__).exception("Lỗi khi lập kế hoạch bất đồng bộ cho run %s: %s", run_id, exc)
            run = session.get(WorkflowRunModel, run_id)
            if run:
                run.status = RunStatus.FAILED.value
                session.commit()


@router.post("", status_code=201)
def create_run(
    req: CreateRunRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Tạo mới một WorkflowRun (Yêu cầu ADMIN_TOKEN)."""
    effective_brief = (req.prompt or req.brief or "").strip()
    if not effective_brief:
        raise HTTPException(
            status_code=422,
            detail=AgentError(
                code=ErrorCode.INVALID_INPUT,
                message="Cần cung cấp 'prompt' hoặc 'brief' để khởi tạo workflow run.",
                retryable=False,
            ).model_dump(),
        )

    run_id = req.run_id or f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
    project_id = req.project_id or f"PRJ-{uuid.uuid4().hex[:6].upper()}"

    existing = WorkflowRunRepository.get_run(session, run_id)
    if existing:
        raise HTTPException(
            status_code=409,
            detail=AgentError(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"Run '{run_id}' đã tồn tại.",
                retryable=False,
            ).model_dump(),
        )

    # Kiểm tra giới hạn số run chạy đồng thời (MAX_CONCURRENT_RUNS)
    active_runs = session.query(func.count(WorkflowRunModel.run_id)).filter(
        WorkflowRunModel.status.in_([
            RunStatus.PLANNING.value,
            RunStatus.RUNNING.value,
            RunStatus.AWAITING_APPROVAL.value,
        ])
    ).scalar() or 0

    if active_runs >= settings.MAX_CONCURRENT_RUNS:
        raise HTTPException(
            status_code=429,
            detail=AgentError(
                code=ErrorCode.DISPATCH_FAILED,
                message=f"Đã đạt giới hạn tối đa số WorkflowRun chạy đồng thời ({active_runs}/{settings.MAX_CONCURRENT_RUNS}). Vui lòng chờ các run hiện tại hoàn tất.",
                retryable=True,
            ).model_dump(),
        )

    initial_status = RunStatus.CREATED.value if req.plan else RunStatus.PLANNING.value
    run = WorkflowRunRepository.create_run(
        session,
        {
            "run_id": run_id,
            "project_id": project_id,
            "brief": effective_brief,
            "plan": req.plan,
            "status": initial_status,
        },
    )

    AuditRepository.record(
        session,
        actor=admin.actor_id,
        action="RUN_CREATED",
        target=run_id,
        run_id=run_id,
        result=initial_status,
        details={"prompt": effective_brief, "project_id": project_id},
    )

    if publisher:
        publisher.publish(
            event_type=SystemEventType.RUN_STARTED,
            payload={"run_id": run_id, "project_id": project_id, "brief": effective_brief, "status": initial_status},
            run_id=run_id,
            session=session,
        )

    session.commit()

    if req.plan and "tasks" in req.plan:
        # Nếu đã có sẵn cấu trúc tasks truyền vào, lưu trực tiếp
        plan_service = PlanService(publisher=publisher)
        plan_service.save_plan_to_db(session=session, run_id=run_id, plan=req.plan)
    elif not req.plan:
        # Kích hoạt lập kế hoạch bất đồng bộ trong background
        background_tasks.add_task(_background_plan_task, run_id, effective_brief, project_id)

    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "status": run.status,
        "created_at": run.created_at.isoformat() if run.created_at else None,
    }


@router.get("/{run_id}")
def get_run(
    run_id: str,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> Dict[str, Any]:
    """Xem thông tin chi tiết và tiến độ của một WorkflowRun."""
    run = WorkflowRunRepository.get_run(session, run_id)
    if not run:
        raise HTTPException(
            status_code=404,
            detail=AgentError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Không tìm thấy WorkflowRun '{run_id}'.",
                retryable=False,
            ).model_dump(),
        )

    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "status": run.status,
        "brief": run.brief,
        "current_stage": run.current_stage,
        "created_at": run.created_at.isoformat() if run.created_at else None,
        "updated_at": run.updated_at.isoformat() if run.updated_at else None,
    }


@router.get("/{run_id}/tasks")
def get_run_tasks(
    run_id: str,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> List[Dict[str, Any]]:
    """Lấy danh sách các task thuộc một WorkflowRun."""
    stmt = select(TaskModel).where(TaskModel.run_id == run_id).order_by(TaskModel.created_at)
    tasks = session.execute(stmt).scalars().all()

    return [
        {
            "task_id": t.task_id,
            "run_id": t.run_id,
            "agent_id": t.agent_id,
            "role": t.role,
            "action": t.action,
            "dispatch_state": t.dispatch_state,
            "attempts": t.attempts,
            "version": t.version,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        }
        for t in tasks
    ]


@router.get("/{run_id}/artifacts", response_model=List[ArtifactRef])
def list_run_artifacts(
    run_id: str,
    type: Optional[str] = None,
    latest: bool = True,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> List[ArtifactRef]:
    """Liệt kê danh sách artifact thuộc về một WorkflowRun (hỗ trợ lọc theo type và latest)."""
    models = artifact_service.list_artifacts(
        session=session,
        run_id=run_id,
        artifact_type=type,
        latest=latest,
    )
    return [model_to_artifact_ref(m) for m in models]


@router.get("/{run_id}/plan")
def get_run_plan(
    run_id: str,
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> Dict[str, Any]:
    """Lấy kế hoạch thực thi (Plan/DAG) của một WorkflowRun."""
    run = WorkflowRunRepository.get_run(session, run_id)
    if not run:
        raise HTTPException(
            status_code=404,
            detail=AgentError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Không tìm thấy WorkflowRun '{run_id}'.",
                retryable=False,
            ).model_dump(),
        )

    return {"run_id": run.run_id, "plan": run.plan or {}}


@router.post("/{run_id}/approve")
def approve_run_gate(
    run_id: str,
    req: ApproveGateRequest = ApproveGateRequest(),
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Phê duyệt Run (từ AWAITING_APPROVAL sang RUNNING) hoặc cổng kiểm duyệt (Gate) cho Run (Yêu cầu ADMIN_TOKEN)."""
    approval_svc = ApprovalService(publisher=publisher)
    try:
        return approval_svc.approve(
            session=session,
            run_id=run_id,
            gate=req.gate,
            decided_by=admin.actor_id,
            notes=req.notes,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=AgentError(code=ErrorCode.INVALID_INPUT, message=str(e), retryable=False).model_dump(),
        )


@router.post("/{run_id}/reject")
def reject_run_gate(
    run_id: str,
    req: RejectGateRequest,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Từ chối cổng kiểm duyệt (Gate) cho Run (Yêu cầu ADMIN_TOKEN)."""
    approval_svc = ApprovalService(publisher=publisher)
    try:
        return approval_svc.reject(
            session=session,
            run_id=run_id,
            gate=req.gate,
            decided_by=admin.actor_id,
            reason=req.reason,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=AgentError(code=ErrorCode.INVALID_INPUT, message=str(e), retryable=False).model_dump(),
        )


@router.post("/{run_id}/cancel")
def cancel_run(
    run_id: str,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Hủy thực thi một WorkflowRun (Yêu cầu ADMIN_TOKEN)."""
    engine = OrchestratorEngine(publisher=publisher)
    try:
        return engine.cancel_run(run_id=run_id, actor=admin.actor_id, session=session)
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=AgentError(code=ErrorCode.INVALID_INPUT, message=str(e), retryable=False).model_dump(),
        )


@router.post("/{run_id}/pause")
def pause_run(
    run_id: str,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Tạm dừng WorkflowRun."""
    engine = OrchestratorEngine(publisher=publisher)
    try:
        return engine.pause_run(run_id=run_id, actor=admin.actor_id, session=session)
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=AgentError(code=ErrorCode.INVALID_INPUT, message=str(e), retryable=False).model_dump(),
        )


@router.post("/{run_id}/resume")
def resume_run(
    run_id: str,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Tiếp tục WorkflowRun."""
    engine = OrchestratorEngine(publisher=publisher)
    try:
        return engine.resume_run(run_id=run_id, actor=admin.actor_id, session=session)
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=AgentError(code=ErrorCode.INVALID_INPUT, message=str(e), retryable=False).model_dump(),
        )


@router.post("/{run_id}/tasks/{task_id}/unblock")
def unblock_task(
    run_id: str,
    task_id: str,
    req: UnblockTaskRequest,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    admin: AdminPrincipal = Depends(require_admin),
) -> Dict[str, Any]:
    """Cung cấp ngữ cảnh bổ sung để giải phóng (unblock) task bị nghẽn (Yêu cầu ADMIN_TOKEN)."""
    engine = OrchestratorEngine(publisher=publisher)
    try:
        return engine.unblock_task(
            run_id=run_id,
            task_id=task_id,
            additional_context=req.additional_context,
            actor=admin.actor_id,
            comment=req.comment,
            session=session,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=404,
            detail=AgentError(code=ErrorCode.INVALID_INPUT, message=str(e), retryable=False).model_dump(),
        )
