"""API trên Worker Host theo bảng giao thức mục 4 (worker/host/api.py).

Các endpoints:
- GET /health: kiểm tra tình trạng Worker.
- GET /agents: danh sách và trạng thái agent trên máy.
- GET /agents/{id}: thông tin chi tiết một agent.
- POST /tasks: nhận DispatchRequest, kiểm tra role/action, trả 202 ngay; 409 nếu agent bận;
  giao trùng task_id trả lại trạng thái cũ hoặc kết quả cũ (idempotency).
- GET /tasks/{id}: trạng thái task.
- POST /tasks/{id}/cancel: hủy task.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.responses import JSONResponse

from agent_contract.enums import AgentRole, ErrorCode, TaskStatus
from agent_contract.models import AgentError, AgentIdentity, TaskResult
from agent_contract.registry import ACTION_MAP, CAPABILITY_REGISTRY
from agent_contract.system_models import DispatchAck, DispatchRequest
from worker.host.config import WorkerHostConfig
from worker.host.outbox import OutboxService
from worker.host.runner import RunnerRegistry

logger = logging.getLogger(__name__)


def create_worker_api(
    config: WorkerHostConfig,
    runner_registry: RunnerRegistry,
    outbox: OutboxService,
) -> FastAPI:
    """Khởi tạo ứng dụng FastAPI cho Worker Host."""
    app = FastAPI(
        title=f"Worker Host [{config.worker_id}]",
        version=config.worker_version,
    )

    # Cờ tạm ngừng nhận việc khi đang graceful shutdown
    app.state.is_shutting_down = False

    def require_auth(authorization: Optional[str] = Header(None)) -> bool:
        """Xác thực Bearer token bí mật dành riêng cho Worker Host."""
        if not authorization or not authorization.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AgentError(
                    code=ErrorCode.PERMISSION_DENIED,
                    message="Thiếu header 'Authorization: Bearer <secret>'.",
                    retryable=False,
                ).model_dump(),
            )
        parts = authorization.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or parts[1] != config.secret:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=AgentError(
                    code=ErrorCode.PERMISSION_DENIED,
                    message="Bearer token xác thực cho Worker Host không hợp lệ.",
                    retryable=False,
                ).model_dump(),
            )
        return True

    @app.get("/health")
    def health_check() -> Dict[str, Any]:
        """Kiểm tra sức khỏe của Worker Host."""
        return {
            "status": "healthy",
            "worker_id": config.worker_id,
            "machine_id": config.machine_id,
            "version": config.worker_version,
            "contract_version": config.contract_version,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "running_tasks": runner_registry.get_running_tasks(),
            "pending_outbox": outbox.get_pending_count(),
        }

    @app.get("/agents", dependencies=[Depends(require_auth)])
    def list_agents() -> List[Dict[str, Any]]:
        """Danh sách và thông tin các agent trên Worker Host này."""
        result = []
        statuses = {s.agent_id: s for s in runner_registry.get_agent_statuses()}
        for agent_cfg in config.agents:
            st = statuses.get(agent_cfg.agent_id)
            result.append({
                "agent_id": agent_cfg.agent_id,
                "role": agent_cfg.role,
                "status": st.status.value if st else "IDLE",
                "current_task": st.current_task if st else None,
                "capabilities": agent_cfg.capabilities,
            })
        return result

    @app.get("/agents/{agent_id}", dependencies=[Depends(require_auth)])
    def get_agent(agent_id: str) -> Dict[str, Any]:
        """Thông tin chi tiết của một agent."""
        agent_cfg = next((a for a in config.agents if a.agent_id == agent_id), None)
        if not agent_cfg:
            raise HTTPException(
                status_code=404,
                detail=AgentError(
                    code=ErrorCode.INVALID_INPUT,
                    message=f"Agent '{agent_id}' không tồn tại trên Worker '{config.worker_id}'.",
                    retryable=False,
                ).model_dump(),
            )
        statuses = {s.agent_id: s for s in runner_registry.get_agent_statuses()}
        st = statuses.get(agent_id)
        return {
            "agent_id": agent_cfg.agent_id,
            "role": agent_cfg.role,
            "status": st.status.value if st else "IDLE",
            "current_task": st.current_task if st else None,
            "capabilities": agent_cfg.capabilities,
        }

    @app.post("/tasks", status_code=202, dependencies=[Depends(require_auth)])
    def dispatch_task(request: DispatchRequest, response: Response) -> Any:
        """Tiếp nhận task điều phối từ Control Node PC1."""
        if app.state.is_shutting_down:
            raise HTTPException(
                status_code=503,
                detail=AgentError(
                    code=ErrorCode.UNKNOWN_ERROR,
                    message=f"Worker '{config.worker_id}' đang trong quá trình tắt sạch, không nhận task mới.",
                    retryable=True,
                ).model_dump(),
            )

        agent_id = request.agent_id
        task_id = request.task_id

        # 1. Kiểm tra Agent có thuộc Worker này không
        agent_cfg = next((a for a in config.agents if a.agent_id == agent_id), None)
        if not agent_cfg:
            logger.warning(f"Từ chối task '{task_id}': Agent '{agent_id}' không thuộc worker '{config.worker_id}'.")
            return JSONResponse(
                status_code=422,
                content=DispatchAck(task_id=task_id, accepted=False, reason="INVALID_ROLE_OR_ACTION").model_dump(),
            )

        # 2. Kiểm tra Role và Action có hợp lệ cho agent đó không
        expected_role_str = agent_cfg.role
        req_role_str = request.role.value if isinstance(request.role, AgentRole) else str(request.role)
        if req_role_str != expected_role_str:
            logger.warning(f"Từ chối task '{task_id}': Role '{req_role_str}' không khớp với role agent '{expected_role_str}'.")
            return JSONResponse(
                status_code=422,
                content=DispatchAck(task_id=task_id, accepted=False, reason="INVALID_ROLE_OR_ACTION").model_dump(),
            )

        # Kiểm tra action có thuộc role đó trong ACTION_MAP không
        role_enum = AgentRole(req_role_str)
        allowed_actions = ACTION_MAP.get(role_enum, {})
        if request.action not in allowed_actions:
            logger.warning(f"Từ chối task '{task_id}': Action '{request.action}' không thuộc role '{req_role_str}'.")
            return JSONResponse(
                status_code=422,
                content=DispatchAck(task_id=task_id, accepted=False, reason="INVALID_ROLE_OR_ACTION").model_dump(),
            )

        # 3. IDEMPOTENCY:
        # Nếu task đã có trong cached result (đã chạy xong trước đó)
        cached_result = outbox.get_cached_result(task_id)
        if cached_result:
            logger.info(f"Task '{task_id}' đã có kết quả trong cache outbox. Trả lại xác nhận 202.")
            response.status_code = 202
            return DispatchAck(task_id=task_id, accepted=True, reason="ALREADY_COMPLETED")

        # Nếu task đang chạy hiện tại
        if task_id in runner_registry.get_running_tasks():
            logger.info(f"Task '{task_id}' hiện đang chạy trên agent '{agent_id}'. Trả lại xác nhận 202.")
            response.status_code = 202
            return DispatchAck(task_id=task_id, accepted=True, reason="ALREADY_RUNNING")

        # 4. Kiểm tra Agent có đang bận task khác không (409 Conflict)
        if runner_registry.is_agent_busy(agent_id):
            logger.info(f"Agent '{agent_id}' đang bận. Từ chối task '{task_id}' với HTTP 409.")
            return JSONResponse(
                status_code=409,
                content=DispatchAck(task_id=task_id, accepted=False, reason="BUSY").model_dump(),
            )

        # 5. Đưa task vào runner để chạy ngầm trong thread
        submitted = runner_registry.submit_task(request)
        if not submitted:
            return JSONResponse(
                status_code=409,
                content=DispatchAck(task_id=task_id, accepted=False, reason="BUSY").model_dump(),
            )

        # Trả về 202 Accepted ngay lập tức
        response.status_code = 202
        return DispatchAck(task_id=task_id, accepted=True)

    @app.get("/tasks/{task_id}", dependencies=[Depends(require_auth)])
    def get_task_status(task_id: str) -> Dict[str, Any]:
        """Xem trạng thái thực thi hiện tại của task."""
        # Kiểm tra đang chạy
        if task_id in runner_registry.get_running_tasks():
            return {"task_id": task_id, "status": "RUNNING"}

        # Kiểm tra trong cache outbox
        cached = outbox.get_cached_result(task_id)
        if cached:
            return {
                "task_id": task_id,
                "status": cached.status.value,
                "summary": cached.summary,
            }

        raise HTTPException(
            status_code=404,
            detail=AgentError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Không tìm thấy thông tin task '{task_id}' trên Worker này.",
                retryable=False,
            ).model_dump(),
        )

    @app.post("/tasks/{task_id}/cancel", dependencies=[Depends(require_auth)])
    def cancel_task(task_id: str) -> Dict[str, Any]:
        """Hủy task đang chạy trên Worker này."""
        cancelled = runner_registry.cancel_task(task_id)
        return {"task_id": task_id, "cancelled": cancelled}

    return app
