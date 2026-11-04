"""Endpoint xử lý kết quả TaskResult từ Worker Host (POST /api/v1/tasks/{id}/result)."""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from agent_contract.models import AgentError, TaskResult
from control.app.api.dependencies import get_db, get_publisher, require_worker
from control.app.queue.events import EventPublisher
from control.app.security.authentication import WorkerPrincipal
from control.app.services.result_handler import ResultHandler, ResultHandlerError

router = APIRouter(prefix="/tasks", tags=["Tasks"])


@router.post("/{task_id}/result")
def submit_task_result(
    task_id: str,
    result: TaskResult,
    session: Session = Depends(get_db),
    publisher: EventPublisher = Depends(get_publisher),
    worker: WorkerPrincipal = Depends(require_worker),
) -> Dict[str, Any]:
    """Tiếp nhận TaskResult từ Worker Host cho một task cụ thể qua ResultHandler."""
    try:
        return ResultHandler.handle_result(
            session=session,
            task_id=task_id,
            result=result,
            worker=worker,
            publisher=publisher,
        )
    except ResultHandlerError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail=AgentError(
                code=e.code,
                message=e.message,
                retryable=False,
            ).model_dump(),
        )
