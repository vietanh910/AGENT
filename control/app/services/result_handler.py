"""Xử lý kết quả TaskResult từ Worker Host nộp về PC1 (Result Handler).

Đặc tả:
- Gọi từ endpoint POST /api/v1/tasks/{id}/result.
- Tính lũy kế (Idempotent): kết quả trùng thì trả 200 OK và bỏ qua.
- Kiểm tra tính hợp lệ của task_id, agent_id, và schema TaskResult.
- Phân quyền Worker: Worker chỉ được gửi kết quả cho task thuộc agent nó sở hữu (403).
- Lưu kết quả vào DB và phát sự kiện thích hợp qua EventPublisher.
- Nếu FAILED:
  + error.retryable=True và còn lượt thử (attempts < max_attempts): chuyển sang RETRY_WAIT (giữ nguyên task_id).
  + error.retryable=True nhưng hết lượt thử: chuyển sang FAILED cuối cùng.
  + error.retryable=False: chuyển sang FAILED ngay lập tức (không retry).
- Nếu BLOCKED: giữ nguyên trạng thái BLOCKED và báo orchestrator.
- Nếu COMPLETED: chuyển sang COMPLETED, giải phóng agent về IDLE.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode, TaskStatus
from agent_contract.models import AgentError, TaskResult
from agent_contract.system_models import DispatchState, SystemEventType
from control.app.database.models import AgentModel, TaskModel, utc_now
from control.app.database.repositories import AuditRepository, TaskRepository
from control.app.orchestrator.guards import WorkflowGuards
from control.app.queue.events import EventPublisher
from control.app.security.authentication import WorkerPrincipal
from control.app.security.permissions import ActionPolicy, PermissionDeniedException
from control.app.services.task_manager import TaskManager

logger = logging.getLogger(__name__)


class ResultHandlerError(Exception):
    """Lỗi xử lý TaskResult."""

    def __init__(self, code: ErrorCode, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class ResultHandler:
    """Xử lý kết quả nộp từ Worker."""

    @classmethod
    def handle_result(
        cls,
        session: Session,
        task_id: str,
        result: TaskResult,
        worker: WorkerPrincipal,
        publisher: Optional[EventPublisher] = None,
    ) -> Dict[str, Any]:
        """Tiếp nhận và xử lý kết quả TaskResult theo đúng quy tắc S5."""
        # 1. Kiểm tra khớp task_id
        if result.task_id != task_id:
            raise ResultHandlerError(
                code=ErrorCode.VALIDATION_ERROR,
                message=f"ID task trên URL ('{task_id}') không khớp với result.task_id ('{result.task_id}').",
                status_code=400,
            )

        # 2. Tìm task trong DB
        task = TaskRepository.get_task(session, task_id)
        if not task:
            raise ResultHandlerError(
                code=ErrorCode.INVALID_INPUT,
                message=f"Không tìm thấy task '{task_id}' trong hệ thống.",
                status_code=404,
            )

        # 3. Kiểm tra bảo mật Worker isolation
        if not worker.can_operate_agent(task.agent_id):
            AuditRepository.record(
                session,
                actor=worker.worker_id,
                action="TASK_RESULT_FORBIDDEN",
                target=task_id,
                result="FORBIDDEN_AGENT_MISMATCH",
                details={
                    "task_agent_id": task.agent_id,
                    "worker_allowed_agents": worker.allowed_agents,
                    "submitted_agent_id": result.agent_id,
                },
            )
            session.commit()
            raise PermissionDeniedException(
                f"Worker '{worker.worker_id}' không có quyền gửi kết quả cho task '{task_id}' "
                f"vì task này được giao cho agent '{task.agent_id}'."
            )

        # 3.5. Kiểm tra ActionPolicy: role của agent có được phép làm action này không
        if not ActionPolicy.is_action_allowed(task.role, task.action):
            AuditRepository.record(
                session,
                actor=worker.worker_id,
                action="ACTION_POLICY_VIOLATION",
                target=task_id,
                run_id=task.run_id,
                result="FORBIDDEN_ACTION",
                details={
                    "role": str(task.role),
                    "action": task.action,
                    "agent_id": task.agent_id,
                },
            )
            session.commit()
            raise PermissionDeniedException(
                f"ActionPolicy từ chối: Role '{task.role}' không được phép thực hiện action '{task.action}'."
            )

        # 4. Kiểm tra IDEMPOTENCY:
        # Nếu task đã ở trạng thái kết thúc (COMPLETED hoặc CANCELLED), bỏ qua an toàn và trả 200
        if task.dispatch_state in (DispatchState.COMPLETED.value, DispatchState.CANCELLED.value):
            logger.info(f"Task '{task_id}' đã ở trạng thái {task.dispatch_state}. Bỏ qua kết quả callback trùng lặp.")
            return {
                "status": "ignored_duplicate",
                "task_id": task_id,
                "dispatch_state": task.dispatch_state,
                "message": f"Task already in terminal state {task.dispatch_state}.",
            }

        # Nếu task đã FAILED và đã hết lượt thử
        if task.dispatch_state == DispatchState.FAILED.value and task.attempts >= task.max_attempts:
            logger.info(f"Task '{task_id}' đã FAILED và hết lượt thử. Bỏ qua kết quả trùng lặp.")
            return {
                "status": "ignored_duplicate",
                "task_id": task_id,
                "dispatch_state": task.dispatch_state,
                "message": "Task already marked as final FAILED.",
            }

        now = utc_now()

        # 5. Xử lý theo từng loại trạng thái kết quả
        if result.status == TaskStatus.COMPLETED:
            # Task thành công hoàn toàn
            task.dispatch_state = DispatchState.COMPLETED.value
            task.result = result.model_dump(mode="json")
            task.updated_at = now
            task.version += 1

            # Giải phóng agent
            agent = session.get(AgentModel, task.agent_id)
            if agent and agent.current_task == task.task_id:
                agent.current_task = None
                if agent.status != "OFFLINE":
                    agent.status = "IDLE"

            AuditRepository.record(
                session,
                actor=worker.worker_id,
                action="TASK_COMPLETED",
                target=task_id,
                run_id=task.run_id,
                result="COMPLETED",
                details={"agent_id": result.agent_id},
            )

            if publisher:
                publisher.publish(
                    event_type=SystemEventType.TASK_COMPLETED.value,
                    run_id=task.run_id,
                    task_id=task_id,
                    agent_id=result.agent_id,
                    payload={"status": "COMPLETED", "agent_id": result.agent_id},
                    session=session,
                )

            session.commit()
            return {"status": "accepted", "task_id": task_id, "dispatch_state": DispatchState.COMPLETED.value}

        elif result.status == TaskStatus.BLOCKED:
            # Task bị nghẽn (cần thêm context / input)
            task.dispatch_state = DispatchState.BLOCKED.value
            task.result = result.model_dump(mode="json")
            if result.error:
                task.error = result.error.model_dump(mode="json")
            task.updated_at = now
            task.version += 1

            # Giải phóng agent hoặc để trạng thái BLOCKED
            agent = session.get(AgentModel, task.agent_id)
            if agent and agent.current_task == task.task_id:
                agent.current_task = None
                if agent.status != "OFFLINE":
                    agent.status = "BLOCKED"

            AuditRepository.record(
                session,
                actor=worker.worker_id,
                action="TASK_BLOCKED",
                target=task_id,
                run_id=task.run_id,
                result="BLOCKED",
                details={"reason": result.error.message if result.error else "Missing context"},
            )

            if publisher:
                publisher.publish(
                    event_type=SystemEventType.TASK_BLOCKED.value,
                    run_id=task.run_id,
                    task_id=task_id,
                    agent_id=task.agent_id,
                    payload={
                        "status": "BLOCKED",
                        "error": result.error.model_dump(mode="json") if result.error else None,
                    },
                    session=session,
                )

            session.commit()
            return {"status": "accepted", "task_id": task_id, "dispatch_state": DispatchState.BLOCKED.value}

        elif result.status == TaskStatus.FAILED:
            error_dict = result.error.model_dump(mode="json") if result.error else {"message": "Unknown task failure"}
            is_retryable = result.error.retryable if result.error else False
            has_attempts_left = task.attempts < task.max_attempts

            # Kiểm tra ngân sách retry cho toàn bộ run
            guards = WorkflowGuards()
            budget_ok, budget_reason = guards.check_run_retry_budget(session, task.run_id)

            if is_retryable and has_attempts_left and budget_ok:
                # Retry được, còn lượt thử và còn ngân sách run -> RETRY_WAIT
                TaskManager.schedule_retry(
                    session=session,
                    task=task,
                    error_details=error_dict,
                    worker_id=worker.worker_id,
                    publisher=publisher,
                )
                session.commit()
                return {"status": "accepted", "task_id": task_id, "dispatch_state": DispatchState.RETRY_WAIT.value}

            elif not budget_ok:
                # Vượt quá ngân sách retry của run -> cách ly vào BLOCKED
                task.dispatch_state = DispatchState.BLOCKED.value
                task.result = result.model_dump(mode="json")
                task.error = {
                    "code": ErrorCode.DISPATCH_FAILED.value,
                    "message": budget_reason or "Vượt quá ngân sách retry của WorkflowRun.",
                    "original_error": error_dict,
                    "retryable": False,
                }
                task.updated_at = now
                task.version += 1

                agent = session.get(AgentModel, task.agent_id)
                if agent and agent.current_task == task.task_id:
                    agent.current_task = None
                    if agent.status != "OFFLINE":
                        agent.status = "IDLE"

                AuditRepository.record(
                    session,
                    actor=worker.worker_id,
                    action="RETRY_BUDGET_EXHAUSTED",
                    target=task_id,
                    run_id=task.run_id,
                    result="BLOCKED",
                    details={"error": error_dict, "reason": budget_reason},
                )
                if publisher:
                    publisher.publish(
                        event_type=SystemEventType.TASK_BLOCKED.value,
                        run_id=task.run_id,
                        task_id=task_id,
                        agent_id=task.agent_id,
                        payload={"status": "BLOCKED", "reason": "RETRY_BUDGET_EXHAUSTED"},
                        session=session,
                    )
                session.commit()
                return {"status": "accepted", "task_id": task_id, "dispatch_state": DispatchState.BLOCKED.value}

            elif not has_attempts_left:
                # Task độc hại liên tục lỗi đạt max_attempts -> cách ly an toàn vào BLOCKED
                task.dispatch_state = DispatchState.BLOCKED.value
                task.result = result.model_dump(mode="json")
                task.error = {
                    "code": ErrorCode.UNKNOWN_ERROR.value,
                    "message": f"POISON_TASK_DETECTED: Task liên tục thất bại và đạt giới hạn max_attempts ({task.attempts}/{task.max_attempts}). Đã cách ly an toàn vào BLOCKED.",
                    "original_error": error_dict,
                    "retryable": False,
                }
                task.updated_at = now
                task.version += 1

                agent = session.get(AgentModel, task.agent_id)
                if agent and agent.current_task == task.task_id:
                    agent.current_task = None
                    if agent.status != "OFFLINE":
                        agent.status = "IDLE"

                AuditRepository.record(
                    session,
                    actor=worker.worker_id,
                    action="POISON_TASK_ISOLATED",
                    target=task_id,
                    run_id=task.run_id,
                    result="BLOCKED",
                    details={"attempts": task.attempts, "max_attempts": task.max_attempts, "error": error_dict},
                )
                if publisher:
                    publisher.publish(
                        event_type=SystemEventType.TASK_BLOCKED.value,
                        run_id=task.run_id,
                        task_id=task_id,
                        agent_id=task.agent_id,
                        payload={"status": "BLOCKED", "reason": "POISON_TASK_ISOLATED"},
                        session=session,
                    )
                session.commit()
                return {"status": "accepted", "task_id": task_id, "dispatch_state": DispatchState.BLOCKED.value}

            else:
                # Không retry được (non-retryable error) -> FAILED vĩnh viễn
                task.dispatch_state = DispatchState.FAILED.value
                task.result = result.model_dump(mode="json")
                task.error = error_dict
                task.updated_at = now
                task.version += 1

                agent = session.get(AgentModel, task.agent_id)
                if agent and agent.current_task == task.task_id:
                    agent.current_task = None
                    if agent.status != "OFFLINE":
                        agent.status = "IDLE"

                AuditRepository.record(
                    session,
                    actor=worker.worker_id,
                    action="TASK_FAILED_FINAL",
                    target=task_id,
                    run_id=task.run_id,
                    result="FAILED",
                    details={
                        "is_retryable": is_retryable,
                        "attempts": task.attempts,
                        "max_attempts": task.max_attempts,
                        "error": error_dict,
                    },
                )

                if publisher:
                    publisher.publish(
                        event_type=SystemEventType.TASK_FAILED.value,
                        run_id=task.run_id,
                        task_id=task_id,
                        agent_id=task.agent_id,
                        payload={"status": "FAILED", "error": error_dict, "final": True},
                        session=session,
                    )

                session.commit()
                return {"status": "accepted", "task_id": task_id, "dispatch_state": DispatchState.FAILED.value}

        # Trạng thái không xác định
        session.commit()
        return {"status": "accepted", "task_id": task_id, "dispatch_state": task.dispatch_state}
