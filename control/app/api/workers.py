"""Endpoint đăng ký và Heartbeat dành cho Worker Host (PC2..PC5)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode
from agent_contract.models import AgentError
from agent_contract.system_models import Heartbeat, WorkerRegistration
from control.app.api.dependencies import get_db, get_publisher, require_worker
from control.app.queue.events import EventPublisher
from control.app.security.authentication import WorkerPrincipal
from control.app.security.permissions import PermissionDeniedException
from control.app.services.registry import RegistryError, RegistryService

router = APIRouter(prefix="/workers", tags=["Workers"])


@router.post("/register")
def register_worker(
    registration: WorkerRegistration,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    worker: WorkerPrincipal = Depends(require_worker),
) -> Dict[str, Any]:
    """Worker Host gửi thông tin đăng ký cùng danh sách Agent đảm nhiệm."""
    # Worker chỉ được đăng ký cho chính worker_id của mình
    if registration.worker_id != worker.worker_id:
        raise PermissionDeniedException(
            f"Bearer token của '{worker.worker_id}' không được dùng để đăng ký cho '{registration.worker_id}'."
        )

    service = RegistryService(session, publisher=publisher)
    try:
        registered_worker = service.register_worker(registration)
        session.commit()
    except RegistryError as e:
        session.rollback()
        raise HTTPException(
            status_code=400,
            detail=AgentError(
                code=ErrorCode.VALIDATION_ERROR,
                message=e.message,
                details=e.details,
                retryable=False,
            ).model_dump(),
        )

    return {
        "status": "registered",
        "worker_id": registered_worker.worker_id,
        "machine_id": registered_worker.machine_id,
        "agents": [a.agent_id for a in registration.agents],
    }


@router.post("/heartbeat")
def worker_heartbeat(
    heartbeat_data: Heartbeat,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    worker: WorkerPrincipal = Depends(require_worker),
) -> Dict[str, Any]:
    """Worker Host gửi nhịp tim định kỳ (10s), kèm trạng thái agent và running tasks."""
    if heartbeat_data.worker_id != worker.worker_id:
        raise PermissionDeniedException(
            f"Bearer token của '{worker.worker_id}' không được dùng để gửi heartbeat cho '{heartbeat_data.worker_id}'."
        )

    service = RegistryService(session, publisher=publisher)
    try:
        updated_worker = service.record_heartbeat(heartbeat_data)
        session.commit()
    except RegistryError as e:
        session.rollback()
        raise HTTPException(
            status_code=404,
            detail=AgentError(
                code=ErrorCode.INVALID_INPUT,
                message=e.message,
                details=e.details,
                retryable=False,
            ).model_dump(),
        )

    return {
        "status": "acknowledged",
        "worker_id": updated_worker.worker_id,
        "server_time": updated_worker.last_heartbeat_at.isoformat() if updated_worker.last_heartbeat_at else None,
    }
