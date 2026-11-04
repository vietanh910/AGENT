"""system_models.py - Các Data Models mở rộng cho giai đoạn System Phase.

Định nghĩa giao thức giao việc bất đồng bộ, heartbeat, đăng ký worker,
quản lý trạng thái WorkflowRun, RunTask, và ánh xạ trạng thái dispatch.
Tuân thủ Pydantic v2 và chuẩn múi giờ UTC.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from agent_contract.enums import AgentRole, AgentStatus, TaskStatus
from agent_contract.models import (
    CONTRACT_VERSION,
    AgentError,
    AgentIdentity,
    TaskRequest,
    TaskResult,
)
from agent_contract.validators import validate_agent_id


class RunStatus(str, Enum):
    """Trạng thái vòng đời của một WorkflowRun trong Control Node."""
    CREATED = "CREATED"
    PLANNING = "PLANNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DispatchState(str, Enum):
    """Trạng thái điều phối chi tiết của Task trong cơ sở dữ liệu PC1."""
    QUEUED = "QUEUED"
    ASSIGNED = "ASSIGNED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


# =============================================================================
# ÁNH XẠ TRẠNG THÁI DISPATCH_STATE -> TASK_STATUS
# =============================================================================
DISPATCH_TO_TASK_STATUS: dict[DispatchState, TaskStatus] = {
    DispatchState.QUEUED: TaskStatus.PENDING,
    DispatchState.ASSIGNED: TaskStatus.PENDING,
    DispatchState.RETRY_WAIT: TaskStatus.PENDING,
    DispatchState.RUNNING: TaskStatus.RUNNING,
    DispatchState.COMPLETED: TaskStatus.COMPLETED,
    DispatchState.FAILED: TaskStatus.FAILED,
    DispatchState.BLOCKED: TaskStatus.BLOCKED,
    DispatchState.CANCELLED: TaskStatus.CANCELLED,
}


class SystemEventType(str, Enum):
    """Các loại sự kiện trong hệ thống điều phối Control Node."""
    WORKER_ONLINE = "WORKER_ONLINE"
    WORKER_OFFLINE = "WORKER_OFFLINE"
    TASK_DISPATCHED = "TASK_DISPATCHED"
    TASK_STARTED = "TASK_STARTED"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_BLOCKED = "TASK_BLOCKED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_DECIDED = "APPROVAL_DECIDED"
    RUN_STARTED = "RUN_STARTED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"


class WorkerRegistration(BaseModel):
    """Bản tin đăng ký của Worker Host khi khởi động gửi tới PC1."""
    worker_id: str
    machine_id: str
    base_url: str
    agents: list[AgentIdentity]
    contract_version: str = CONTRACT_VERSION
    worker_version: str = "1.0.0"


class HeartbeatAgentStatus(BaseModel):
    """Trạng thái từng agent trong bản tin Heartbeat."""
    agent_id: str
    status: AgentStatus
    current_task: str | None = None

    @field_validator("agent_id")
    @classmethod
    def check_agent_id(cls, v: str) -> str:
        return validate_agent_id(v)


class Heartbeat(BaseModel):
    """Bản tin nhịp tim định kỳ (10s) của Worker Host gửi về PC1."""
    worker_id: str
    agents: list[HeartbeatAgentStatus] = Field(default_factory=list)
    running_tasks: list[str] = Field(default_factory=list)
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DispatchRequest(TaskRequest):
    """Yêu cầu điều phối tác vụ gửi tới Worker Host kèm callback_url và lần thử."""
    callback_url: str
    attempt: int = 1


class DispatchAck(BaseModel):
    """Phản hồi xác nhận tiếp nhận việc từ Worker Host (HTTP 202)."""
    task_id: str
    accepted: bool
    reason: str | None = None


class WorkflowRun(BaseModel):
    """Thực thể WorkflowRun lưu trữ trong cơ sở dữ liệu Control Node."""
    run_id: str
    project_id: str
    status: RunStatus = RunStatus.CREATED
    brief: str
    plan: dict[str, Any] = Field(default_factory=dict)
    current_stage: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    linear_project_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RunTask(BaseModel):
    """Thực thể RunTask thuộc WorkflowRun lưu trong cơ sở dữ liệu Control Node."""
    task_id: str
    run_id: str
    agent_id: str
    role: AgentRole
    action: str
    instruction: str
    context: dict[str, Any] = Field(default_factory=dict)
    dispatch_state: DispatchState = DispatchState.QUEUED
    attempts: int = 0
    max_attempts: int = 3
    timeout_seconds: int = 1000
    deadline: datetime | None = None
    result: TaskResult | None = None
    error: AgentError | None = None
    linear_issue_id: str | None = None
    version: int = 1
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("agent_id")
    @classmethod
    def check_agent_id(cls, v: str) -> str:
        return validate_agent_id(v)


class Approval(BaseModel):
    """Thông tin phê duyệt tại các cổng duyệt (gate)."""
    gate: str
    status: str = "PENDING"
    decided_by: str | None = None
    decided_at: datetime | None = None
    reason: str | None = None
    run_id: str | None = None


class AuditEvent(BaseModel):
    """Sự kiện kiểm toán (Audit) ghi nhận mọi hành động nhạy cảm trong hệ thống."""
    agent_id: str | None = None
    actor: str | None = None
    action: str
    target: str | None = None
    task_id: str | None = None
    run_id: str | None = None
    result: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = Field(default_factory=dict)
