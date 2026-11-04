"""Quản lý vòng đời tác vụ (Task Manager) cho PC1 Control Node.

Trách nhiệm:
- Tạo task và các quan hệ phụ thuộc (DAG) từ kế hoạch thực thi.
- Tính toán danh sách task READY (mọi phụ thuộc đã COMPLETED, trạng thái QUEUED hoặc RETRY_WAIT đã tới hạn).
- Chuyển đổi trạng thái dispatch_state theo ma trận chuyển đổi hợp lệ.
- Tính toán deadline từ timeout_seconds và tính toán thời gian chờ backoff khi thử lại.
- Ghi nhận lịch sử các lần dispatch (task_attempts).
- Xử lý hủy task và hủy toàn bộ workflow run.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, TaskStatus
from agent_contract.registry import AGENT_REGISTRY_DEFAULT
from agent_contract.system_models import DispatchState, RunStatus, SystemEventType
from control.app.database.models import (
    AgentModel,
    AuditLogModel,
    TaskAttemptModel,
    TaskDependencyModel,
    TaskModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import (
    AuditRepository,
    InvalidStateTransitionError,
    TaskNotFoundError,
    TaskRepository,
    VersionConflictError,
    WorkflowRunRepository,
)
from control.app.queue.events import EventPublisher

logger = logging.getLogger(__name__)


class TaskManagerError(Exception):
    """Lỗi chung của TaskManager."""


class TaskManager:
    """Service điều phối vòng đời của các Task trên PC1 Control Node."""

    @staticmethod
    def calculate_deadline(timeout_seconds: int) -> datetime:
        """Tính thời điểm deadline từ số giây timeout."""
        return utc_now() + timedelta(seconds=timeout_seconds)

    @staticmethod
    def calculate_backoff_delay(attempt: int, base_seconds: float = 2.0, max_seconds: float = 60.0) -> float:
        """Tính thời gian chờ backoff lũy thừa có chặn trần: min(base * 2^(attempt - 1), max)."""
        attempt = max(1, attempt)
        delay = base_seconds * (2 ** (attempt - 1))
        return min(delay, max_seconds)

    @staticmethod
    def resolve_agent_id_for_role(role: AgentRole | str) -> str:
        """Tìm agent_id mặc định theo role (v1: mỗi role có đúng một agent trong AGENT_REGISTRY_DEFAULT)."""
        role_val = role.value if isinstance(role, AgentRole) else str(role)
        if role_val == "GATE":
            return "system-gate"
        for entry in AGENT_REGISTRY_DEFAULT:
            entry_role = entry["role"].value if isinstance(entry["role"], AgentRole) else str(entry["role"])
            if entry_role == role_val:
                return entry["agent_id"]
        raise TaskManagerError(f"Không tìm thấy agent phụ trách vai trò '{role_val}' trong hệ thống.")

    @classmethod
    def create_tasks_from_plan(
        cls,
        session: Session,
        run_id: str,
        tasks_data: List[dict[str, Any]],
        dependencies: Optional[List[Tuple[str, str]]] = None,
    ) -> List[TaskModel]:
        """Tạo danh sách task và các quan hệ phụ thuộc trong cơ sở dữ liệu từ kế hoạch.

        Args:
            session: SQLAlchemy session đang mở.
            run_id: Mã workflow run.
            tasks_data: Danh sách dict thông tin task (task_id, role, action, instruction, context, timeout_seconds, ...).
            dependencies: Danh sách tuple (task_id, depends_on_task_id).

        Returns:
            Danh sách TaskModel đã được tạo và lưu vào DB.
        """
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise TaskManagerError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        created_tasks: List[TaskModel] = []
        now = utc_now()

        for tdata in tasks_data:
            tid = tdata.get("task_id") or tdata.get("id") or f"task-{uuid.uuid4().hex[:10]}"
            role_val = tdata["role"].value if isinstance(tdata["role"], AgentRole) else str(tdata["role"])
            agent_id = tdata.get("agent_id") or cls.resolve_agent_id_for_role(role_val)
            timeout = tdata.get("timeout_seconds", 1000)

            task_payload = {
                "task_id": tid,
                "run_id": run_id,
                "agent_id": agent_id,
                "role": role_val,
                "action": tdata["action"],
                "instruction": tdata["instruction"],
                "context": tdata.get("context", {}),
                "dispatch_state": DispatchState.QUEUED.value,
                "attempts": 0,
                "max_attempts": tdata.get("max_attempts", 3),
                "timeout_seconds": timeout,
                "deadline": None,
                "created_at": now,
                "updated_at": now,
            }
            task = TaskRepository.create_task(session, task_payload)
            created_tasks.append(task)

        # Tạo các quan hệ phụ thuộc
        all_deps: List[Tuple[str, str]] = list(dependencies) if dependencies else []
        for tdata in tasks_data:
            tid = tdata.get("task_id") or tdata.get("id")
            for dep in tdata.get("depends_on", []):
                if dep and (tid, dep) not in all_deps:
                    all_deps.append((tid, dep))

        for tid, dep_id in all_deps:
            TaskRepository.add_dependency(session, task_id=tid, depends_on_task_id=dep_id)

        session.flush()
        return created_tasks

    @staticmethod
    def get_ready_tasks(session: Session, run_id: Optional[str] = None) -> List[TaskModel]:
        """Lấy danh sách task READY để giao việc.

        Task READY khi:
        1. Trạng thái là QUEUED hoặc (RETRY_WAIT và updated_at <= now).
        2. Tất cả các phụ thuộc trong task_dependencies đều có dispatch_state == COMPLETED.
        """
        return TaskRepository.get_ready_tasks(session, run_id=run_id)

    @staticmethod
    def record_attempt(
        session: Session,
        task_id: str,
        attempt_number: int,
        worker_id: Optional[str],
        status: str,
        error: Optional[dict[str, Any]] = None,
        duration_ms: Optional[int] = None,
    ) -> TaskAttemptModel:
        """Ghi nhận một bản ghi lịch sử dispatch vào bảng task_attempts."""
        attempt = TaskAttemptModel(
            attempt_id=f"att-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            attempt_number=attempt_number,
            worker_id=worker_id,
            status=status,
            error=error,
            duration_ms=duration_ms,
            dispatched_at=utc_now(),
        )
        session.add(attempt)
        session.flush()
        return attempt

    @staticmethod
    def transition_task_state(
        session: Session,
        task_id: str,
        new_state: DispatchState | str,
        expected_version: int,
        result: Optional[dict[str, Any]] = None,
        error: Optional[dict[str, Any]] = None,
        increment_attempts: bool = False,
    ) -> TaskModel:
        """Chuyển đổi trạng thái dispatch_state an toàn qua Optimistic Locking."""
        return TaskRepository.update_task_state(
            session=session,
            task_id=task_id,
            new_state=new_state,
            expected_version=expected_version,
            result=result,
            error=error,
            increment_attempts=increment_attempts,
        )

    @classmethod
    def schedule_retry(
        cls,
        session: Session,
        task: TaskModel,
        error_details: dict[str, Any],
        worker_id: Optional[str] = None,
        publisher: Optional[EventPublisher] = None,
    ) -> TaskModel:
        """Lên lịch thử lại (RETRY_WAIT) cho task có lỗi retryable và còn lượt thử."""
        now = utc_now()
        delay_seconds = cls.calculate_backoff_delay(task.attempts)
        next_ready_at = now + timedelta(seconds=delay_seconds)

        updated_task = cls.transition_task_state(
            session=session,
            task_id=task.task_id,
            new_state=DispatchState.RETRY_WAIT,
            expected_version=task.version,
            error=error_details,
        )
        # updated_at đại diện cho mốc thời gian task sẵn sàng thử lại
        updated_task.updated_at = next_ready_at

        # Giải phóng agent tương ứng
        agent = session.get(AgentModel, task.agent_id)
        if agent and agent.current_task == task.task_id:
            agent.current_task = None
            if agent.status != "OFFLINE":
                agent.status = "IDLE"

        cls.record_attempt(
            session=session,
            task_id=task.task_id,
            attempt_number=task.attempts,
            worker_id=worker_id,
            status="RETRY_WAIT",
            error=error_details,
        )

        AuditRepository.record(
            session=session,
            actor="SYSTEM",
            action="TASK_SCHEDULED_RETRY",
            target=task.task_id,
            run_id=task.run_id,
            result="RETRY_WAIT",
            details={
                "attempt": task.attempts,
                "delay_seconds": delay_seconds,
                "next_ready_at": next_ready_at.isoformat(),
            },
        )

        if publisher:
            publisher.publish(
                event_type=SystemEventType.TASK_FAILED.value,
                run_id=task.run_id,
                task_id=task.task_id,
                agent_id=task.agent_id,
                payload={
                    "status": "RETRY_WAIT",
                    "attempt": task.attempts,
                    "delay_seconds": delay_seconds,
                    "error": error_details,
                },
                session=session,
            )

        session.flush()
        return updated_task

    @classmethod
    def cancel_task(
        cls,
        session: Session,
        task_id: str,
        reason: str = "Hủy bởi người dùng",
        actor: str = "ADMIN",
        publisher: Optional[EventPublisher] = None,
    ) -> TaskModel:
        """Hủy một tác vụ cụ thể."""
        task = TaskRepository.get_task(session, task_id)
        if not task:
            raise TaskNotFoundError(f"Không tìm thấy task '{task_id}'.")

        if task.dispatch_state in (DispatchState.COMPLETED.value, DispatchState.CANCELLED.value):
            return task

        updated_task = cls.transition_task_state(
            session=session,
            task_id=task.task_id,
            new_state=DispatchState.CANCELLED,
            expected_version=task.version,
            error={"code": "CANCELLED", "message": reason, "retryable": False},
        )

        # Giải phóng agent
        agent = session.get(AgentModel, task.agent_id)
        if agent and agent.current_task == task.task_id:
            agent.current_task = None
            if agent.status != "OFFLINE":
                agent.status = "IDLE"

        AuditRepository.record(
            session=session,
            actor=actor,
            action="TASK_CANCELLED",
            target=task_id,
            run_id=task.run_id,
            result="CANCELLED",
            details={"reason": reason},
        )

        if publisher:
            publisher.publish(
                event_type=SystemEventType.TASK_FAILED.value,
                run_id=task.run_id,
                task_id=task_id,
                agent_id=task.agent_id,
                payload={"status": "CANCELLED", "reason": reason},
                session=session,
            )

        session.flush()
        return updated_task

    @classmethod
    def cancel_run(
        cls,
        session: Session,
        run_id: str,
        reason: str = "Hủy quy trình bởi người dùng",
        actor: str = "ADMIN",
        publisher: Optional[EventPublisher] = None,
    ) -> WorkflowRunModel:
        """Hủy toàn bộ một WorkflowRun và tất cả các task chưa hoàn thành thuộc run đó."""
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise TaskManagerError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        # Hủy tất cả các task chưa ở trạng thái kết thúc
        stmt = select(TaskModel).where(
            TaskModel.run_id == run_id,
            TaskModel.dispatch_state.notin_([DispatchState.COMPLETED.value, DispatchState.CANCELLED.value]),
        )
        active_tasks = session.execute(stmt).scalars().all()
        for t in active_tasks:
            cls.cancel_task(
                session=session,
                task_id=t.task_id,
                reason=f"Workflow run '{run_id}' đã bị hủy: {reason}",
                actor=actor,
                publisher=publisher,
            )

        run.status = RunStatus.CANCELLED.value
        run.completed_at = utc_now()
        run.updated_at = utc_now()

        AuditRepository.record(
            session=session,
            actor=actor,
            action="RUN_CANCELLED",
            target=run_id,
            run_id=run_id,
            result="CANCELLED",
            details={"reason": reason},
        )

        if publisher:
            publisher.publish(
                event_type=SystemEventType.RUN_FAILED.value,
                run_id=run_id,
                payload={"status": "CANCELLED", "reason": reason},
                session=session,
            )

        session.flush()
        return run
