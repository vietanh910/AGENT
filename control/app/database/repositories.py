"""control/app/database/repositories.py - Toàn bộ truy xuất cơ sở dữ liệu tập trung cho PC1 Control Node.

Tuân thủ:
- Không có RETURNING (đặc thù MySQL): ghi xong đọc lại trong cùng giao dịch.
- Xử lý concurrency: SELECT ... FOR UPDATE SKIP LOCKED khi giao việc và kiểm tra version (khóa lạc quan).
- Chuyển dispatch_state theo bảng chuyển trạng thái hợp lệ, ném InvalidStateTransitionError nếu vi phạm.
- Hàm tính danh sách task READY: mọi phụ thuộc đã COMPLETED, state là QUEUED hoặc RETRY_WAIT đã tới hạn.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from agent_contract.system_models import DispatchState, RunStatus
from control.app.database.models import (
    AgentEventModel,
    AgentModel,
    ApprovalModel,
    ArtifactModel,
    AuditLogModel,
    LinearLinkModel,
    TaskAttemptModel,
    TaskDependencyModel,
    TaskModel,
    WorkerModel,
    WorkflowRunModel,
    utc_now,
)

logger = logging.getLogger(__name__)


class DatabaseRepositoryError(Exception):
    """Lỗi cơ bản tầng Repository."""


class VersionConflictError(DatabaseRepositoryError):
    """Lỗi xung đột phiên bản (Optimistic Locking)."""


class InvalidStateTransitionError(DatabaseRepositoryError):
    """Lỗi chuyển đổi trạng thái dispatch_state không hợp lệ."""


class TaskNotFoundError(DatabaseRepositoryError):
    """Không tìm thấy task yêu cầu."""


# =============================================================================
# BẢNG CHUYỂN ĐỔI TRẠNG THÁI HỢP LỆ CHO DISPATCH_STATE
# =============================================================================
VALID_DISPATCH_TRANSITIONS: dict[str, set[str]] = {
    DispatchState.QUEUED.value: {
        DispatchState.ASSIGNED.value,
        DispatchState.RETRY_WAIT.value,
        DispatchState.FAILED.value,
        DispatchState.CANCELLED.value,
    },
    DispatchState.ASSIGNED.value: {
        DispatchState.RUNNING.value,
        DispatchState.QUEUED.value,
        DispatchState.RETRY_WAIT.value,
        DispatchState.FAILED.value,
        DispatchState.CANCELLED.value,
    },
    DispatchState.RUNNING.value: {
        DispatchState.COMPLETED.value,
        DispatchState.FAILED.value,
        DispatchState.BLOCKED.value,
        DispatchState.RETRY_WAIT.value,
        DispatchState.CANCELLED.value,
    },
    DispatchState.RETRY_WAIT.value: {
        DispatchState.ASSIGNED.value,
        DispatchState.QUEUED.value,
        DispatchState.FAILED.value,
        DispatchState.CANCELLED.value,
    },
    DispatchState.BLOCKED.value: {
        DispatchState.QUEUED.value,    # Khi unblock
        DispatchState.FAILED.value,
        DispatchState.CANCELLED.value,
    },
    DispatchState.COMPLETED.value: set(),   # Trạng thái kết thúc
    DispatchState.FAILED.value: {
        DispatchState.RETRY_WAIT.value,     # Cho phép retry nếu còn lượt
    },
    DispatchState.CANCELLED.value: set(),  # Trạng thái kết thúc
}


def validate_dispatch_transition(current_state: str, new_state: str) -> None:
    """Kiểm tra tính hợp lệ của việc chuyển đổi dispatch_state."""
    if current_state == new_state:
        return
    allowed = VALID_DISPATCH_TRANSITIONS.get(current_state, set())
    if new_state not in allowed:
        raise InvalidStateTransitionError(
            f"Chuyển trạng thái không hợp lệ từ '{current_state}' sang '{new_state}'. "
            f"Các trạng thái được phép: {sorted(allowed) if allowed else 'Không có (trạng thái kết thúc)'}"
        )


# =============================================================================
# 1. TASK REPOSITORY
# =============================================================================
class TaskRepository:
    """Quản lý dữ liệu và vòng đời của Task trong cơ sở dữ liệu."""

    @staticmethod
    def create_task(session: Session, task_data: dict[str, Any]) -> TaskModel:
        """Tạo task mới, ghi xong đọc lại trong cùng giao dịch (thay thế RETURNING)."""
        task = TaskModel(
            task_id=task_data["task_id"],
            run_id=task_data["run_id"],
            agent_id=task_data["agent_id"],
            role=task_data["role"] if isinstance(task_data["role"], str) else task_data["role"].value,
            action=task_data["action"],
            instruction=task_data["instruction"],
            context=task_data.get("context", {}),
            dispatch_state=task_data.get("dispatch_state", DispatchState.QUEUED.value),
            attempts=task_data.get("attempts", 0),
            max_attempts=task_data.get("max_attempts", 3),
            timeout_seconds=task_data.get("timeout_seconds", 300),
            deadline=task_data.get("deadline"),
            result=task_data.get("result"),
            error=task_data.get("error"),
            linear_issue_id=task_data.get("linear_issue_id"),
            version=1,
            created_at=task_data.get("created_at") or utc_now(),
            updated_at=task_data.get("updated_at") or utc_now(),
        )
        session.add(task)
        session.flush()

        # Đọc lại trong cùng giao dịch
        refreshed = session.get(TaskModel, task.task_id)
        assert refreshed is not None
        return refreshed

    @staticmethod
    def get_task(session: Session, task_id: str) -> TaskModel | None:
        """Lấy thông tin task theo task_id."""
        return session.get(TaskModel, task_id)

    @staticmethod
    def add_dependency(session: Session, task_id: str, depends_on_task_id: str) -> TaskDependencyModel:
        """Thêm một cạnh phụ thuộc giữa 2 task."""
        dep = TaskDependencyModel(
            task_id=task_id,
            depends_on_task_id=depends_on_task_id,
            created_at=utc_now(),
        )
        session.add(dep)
        session.flush()
        return dep

    @staticmethod
    def update_task_state(
        session: Session,
        task_id: str,
        new_state: str | DispatchState,
        expected_version: int,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
        increment_attempts: bool = False,
    ) -> TaskModel:
        """Cập nhật trạng thái task có kiểm tra version lạc quan (Optimistic Locking) và transition."""
        new_state_val = new_state.value if isinstance(new_state, DispatchState) else new_state

        task = session.get(TaskModel, task_id)
        if not task:
            raise TaskNotFoundError(f"Không tìm thấy task '{task_id}' để cập nhật.")

        validate_dispatch_transition(task.dispatch_state, new_state_val)

        update_values: dict[str, Any] = {
            "dispatch_state": new_state_val,
            "version": expected_version + 1,
            "updated_at": utc_now(),
        }
        if result is not None:
            update_values["result"] = result
        if error is not None:
            update_values["error"] = error
        if increment_attempts:
            update_values["attempts"] = task.attempts + 1

        stmt = (
            update(TaskModel)
            .where(TaskModel.task_id == task_id, TaskModel.version == expected_version)
            .values(**update_values)
        )
        res = session.execute(stmt)
        if res.rowcount == 0:
            raise VersionConflictError(
                f"Xung đột version khi cập nhật task '{task_id}': kỳ vọng version {expected_version}, "
                f"nhưng version thực tế trong DB đã bị thay đổi bởi tiến trình khác."
            )
        session.flush()

        # Đọc lại dữ liệu đã cập nhật trong cùng transaction
        refreshed = session.get(TaskModel, task_id)
        assert refreshed is not None
        return refreshed

    @staticmethod
    def get_ready_tasks(session: Session, run_id: str | None = None) -> list[TaskModel]:
        """Tính toán danh sách task READY:

        1. dispatch_state là QUEUED hoặc (RETRY_WAIT và updated_at <= now).
        2. Mọi phụ thuộc trong task_dependencies đều có dispatch_state == 'COMPLETED'.
        """
        now = utc_now()

        # Subquery: các phụ thuộc CHƯA hoàn thành
        uncompleted_deps = (
            select(TaskDependencyModel.task_id)
            .join(TaskModel, TaskModel.task_id == TaskDependencyModel.depends_on_task_id)
            .where(TaskModel.dispatch_state != DispatchState.COMPLETED.value)
        )

        query = select(TaskModel).where(
            (TaskModel.dispatch_state == DispatchState.QUEUED.value)
            | ((TaskModel.dispatch_state == DispatchState.RETRY_WAIT.value) & (TaskModel.updated_at <= now))
        ).where(
            ~TaskModel.task_id.in_(uncompleted_deps)
        )

        if run_id:
            query = query.where(TaskModel.run_id == run_id)

        query = query.order_by(TaskModel.created_at.asc())
        return list(session.scalars(query).all())

    @staticmethod
    def claim_next_ready_task(
        session: Session,
        agent_id: str | None = None,
        run_id: str | None = None,
    ) -> TaskModel | None:
        """Chọn và chiếm quyền thực thi (claim) một task READY bằng SELECT ... FOR UPDATE SKIP LOCKED.

        Đảm bảo khi hai worker cùng tranh chấp, chỉ duy nhất một worker thắng.
        """
        now = utc_now()

        uncompleted_deps = (
            select(TaskDependencyModel.task_id)
            .join(TaskModel, TaskModel.task_id == TaskDependencyModel.depends_on_task_id)
            .where(TaskModel.dispatch_state != DispatchState.COMPLETED.value)
        )

        stmt = (
            select(TaskModel)
            .where(TaskModel.dispatch_state == DispatchState.QUEUED.value)
            .where(~TaskModel.task_id.in_(uncompleted_deps))
        )

        if agent_id:
            stmt = stmt.where(TaskModel.agent_id == agent_id)
        if run_id:
            stmt = stmt.where(TaskModel.run_id == run_id)

        stmt = stmt.order_by(TaskModel.created_at.asc()).limit(1).with_for_update(skip_locked=True)

        task = session.scalars(stmt).first()
        if not task:
            return None

        # Chuyển ngay sang ASSIGNED và tăng version
        task.dispatch_state = DispatchState.ASSIGNED.value
        task.version += 1
        task.updated_at = now
        session.flush()
        return task

    @staticmethod
    def record_attempt(
        session: Session,
        task_id: str,
        attempt_number: int,
        status: str,
        worker_id: str | None = None,
        error: dict[str, Any] | None = None,
        duration_ms: int | None = None,
    ) -> TaskAttemptModel:
        """Ghi nhận một lượt dispatch attempt cho task."""
        attempt = TaskAttemptModel(
            attempt_id=f"ATT-{uuid.uuid4().hex[:12]}",
            task_id=task_id,
            attempt_number=attempt_number,
            dispatched_at=utc_now(),
            finished_at=utc_now() if status in ("COMPLETED", "FAILED") else None,
            worker_id=worker_id,
            status=status,
            error=error,
            duration_ms=duration_ms,
        )
        session.add(attempt)
        session.flush()
        return attempt


# =============================================================================
# 2. WORKFLOW RUN REPOSITORY
# =============================================================================
class WorkflowRunRepository:
    """Quản lý các lần thực thi WorkflowRun."""

    @staticmethod
    def create_run(session: Session, run_data: dict[str, Any]) -> WorkflowRunModel:
        """Tạo mới một WorkflowRun."""
        run = WorkflowRunModel(
            run_id=run_data["run_id"],
            project_id=run_data["project_id"],
            status=run_data.get("status", RunStatus.CREATED.value),
            brief=run_data["brief"],
            plan=run_data.get("plan"),
            current_stage=run_data.get("current_stage"),
            created_at=run_data.get("created_at") or utc_now(),
            updated_at=run_data.get("updated_at") or utc_now(),
            linear_project_id=run_data.get("linear_project_id"),
            run_metadata=run_data.get("metadata"),
        )
        session.add(run)
        session.flush()
        return session.get(WorkflowRunModel, run.run_id)  # type: ignore[return-value]

    @staticmethod
    def get_run(session: Session, run_id: str) -> WorkflowRunModel | None:
        """Lấy run theo ID."""
        return session.get(WorkflowRunModel, run_id)

    @staticmethod
    def update_run_status(session: Session, run_id: str, status: str | RunStatus) -> WorkflowRunModel:
        """Cập nhật trạng thái run."""
        status_val = status.value if isinstance(status, RunStatus) else status
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            raise DatabaseRepositoryError(f"Không tìm thấy WorkflowRun '{run_id}'")
        run.status = status_val
        run.updated_at = utc_now()
        if status_val in (RunStatus.COMPLETED.value, RunStatus.FAILED.value, RunStatus.CANCELLED.value):
            run.completed_at = utc_now()
        session.flush()
        return run


# =============================================================================
# 3. WORKER REPOSITORY
# =============================================================================
class WorkerRepository:
    """Quản lý Worker Hosts đăng ký và nhịp tim Heartbeat."""

    @staticmethod
    def register_worker(session: Session, worker_data: dict[str, Any]) -> WorkerModel:
        """Đăng ký mới hoặc cập nhật Worker Host."""
        worker = session.get(WorkerModel, worker_data["worker_id"])
        now = utc_now()
        if not worker:
            worker = WorkerModel(
                worker_id=worker_data["worker_id"],
                machine_id=worker_data["machine_id"],
                base_url=worker_data["base_url"],
                status="ONLINE",
                contract_version=worker_data.get("contract_version", "1.2.0"),
                worker_version=worker_data.get("worker_version", "1.0.0"),
                last_heartbeat_at=now,
                registered_at=now,
                updated_at=now,
            )
            session.add(worker)
        else:
            worker.machine_id = worker_data["machine_id"]
            worker.base_url = worker_data["base_url"]
            worker.status = "ONLINE"
            worker.last_heartbeat_at = now
            worker.updated_at = now

        session.flush()
        return worker

    @staticmethod
    def record_heartbeat(session: Session, worker_id: str) -> WorkerModel | None:
        """Ghi nhận nhịp tim heartbeat từ Worker Host."""
        worker = session.get(WorkerModel, worker_id)
        if worker:
            now = utc_now()
            worker.last_heartbeat_at = now
            worker.status = "ONLINE"
            worker.updated_at = now
            session.flush()
        return worker

    @staticmethod
    def get_stale_workers(session: Session, timeout_seconds: int = 45) -> list[WorkerModel]:
        """Tìm các Worker quá hạn heartbeat (bị coi là OFFLINE)."""
        now = utc_now()
        stmt = (
            select(WorkerModel)
            .where(WorkerModel.status == "ONLINE")
            .where(
                (WorkerModel.last_heartbeat_at.is_(None))
                | (func.timestampdiff(func.literal_column("SECOND"), WorkerModel.last_heartbeat_at, now) > timeout_seconds)
            )
        )
        return list(session.scalars(stmt).all())


# =============================================================================
# 4. AGENT REPOSITORY
# =============================================================================
class AgentRepository:
    """Quản lý thông tin và trạng thái 10 Agent."""

    @staticmethod
    def upsert_agent(session: Session, agent_data: dict[str, Any]) -> AgentModel:
        """Thêm hoặc cập nhật trạng thái Agent."""
        agent = session.get(AgentModel, agent_data["agent_id"])
        now = utc_now()
        role_val = agent_data["role"] if isinstance(agent_data["role"], str) else agent_data["role"].value

        if not agent:
            agent = AgentModel(
                agent_id=agent_data["agent_id"],
                role=role_val,
                machine=agent_data["machine"],
                runner=agent_data["runner"],
                status=agent_data.get("status", "IDLE"),
                current_task=agent_data.get("current_task"),
                capabilities=agent_data.get("capabilities", []),
                updated_at=now,
            )
            session.add(agent)
        else:
            agent.role = role_val
            agent.machine = agent_data["machine"]
            agent.runner = agent_data["runner"]
            agent.status = agent_data.get("status", agent.status)
            agent.current_task = agent_data.get("current_task")
            agent.capabilities = agent_data.get("capabilities", agent.capabilities)
            agent.updated_at = now

        session.flush()
        return agent

    @staticmethod
    def get_agent(session: Session, agent_id: str) -> AgentModel | None:
        """Lấy agent theo ID."""
        return session.get(AgentModel, agent_id)

    @staticmethod
    def list_agents(session: Session) -> list[AgentModel]:
        """Liệt kê toàn bộ Agent trong hệ thống."""
        return list(session.scalars(select(AgentModel).order_by(AgentModel.agent_id.asc())).all())


# =============================================================================
# 5. ARTIFACT REPOSITORY
# =============================================================================
class ArtifactRepository:
    """Quản lý kho Artifact tập trung."""

    @staticmethod
    def save_artifact(session: Session, data: dict[str, Any]) -> ArtifactModel:
        """Lưu siêu dữ liệu Artifact."""
        artifact = ArtifactModel(
            artifact_id=data["artifact_id"],
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            name=data["name"],
            type=data["type"] if isinstance(data["type"], str) else data["type"].value,
            version=data.get("version", "1.0"),
            uri=data["uri"],
            checksum=data.get("checksum"),
            created_by=data["created_by"],
            created_at=data.get("created_at") or utc_now(),
            metadata_json=data.get("metadata", {}),
        )
        session.add(artifact)
        session.flush()
        return artifact

    @staticmethod
    def get_artifact(session: Session, artifact_id: str) -> ArtifactModel | None:
        """Lấy artifact theo ID."""
        return session.get(ArtifactModel, artifact_id)

    @staticmethod
    def list_by_run(
        session: Session,
        run_id: str,
        artifact_type: str | None = None,
        latest: bool = False,
    ) -> list[ArtifactModel]:
        """Liệt kê artifact của một run, hỗ trợ lọc theo loại và lấy bản mới nhất."""
        stmt = select(ArtifactModel).where(ArtifactModel.run_id == run_id)
        if artifact_type:
            stmt = stmt.where(ArtifactModel.type == artifact_type)

        stmt = stmt.order_by(ArtifactModel.created_at.desc())
        all_artifacts = list(session.scalars(stmt).all())

        if not latest:
            return all_artifacts

        # Nhóm theo type và lấy bản mới nhất cho mỗi loại
        latest_by_type: dict[str, ArtifactModel] = {}
        for art in all_artifacts:
            if art.type not in latest_by_type:
                latest_by_type[art.type] = art
        return list(latest_by_type.values())

    @staticmethod
    def get_latest_by_type(
        session: Session,
        run_id: str,
        artifact_type: str,
    ) -> ArtifactModel | None:
        """Lấy bản artifact mới nhất theo loại trong một run."""
        stmt = (
            select(ArtifactModel)
            .where(ArtifactModel.run_id == run_id, ArtifactModel.type == artifact_type)
            .order_by(ArtifactModel.created_at.desc())
            .limit(1)
        )
        return session.scalars(stmt).first()


# =============================================================================
# 6. APPROVAL REPOSITORY
# =============================================================================
class ApprovalRepository:
    """Quản lý phê duyệt tại các cổng duyệt (gate)."""

    @staticmethod
    def create_approval(session: Session, data: dict[str, Any]) -> ApprovalModel:
        """Tạo yêu cầu phê duyệt mới."""
        appr = ApprovalModel(
            approval_id=data.get("approval_id") or f"APPR-{uuid.uuid4().hex[:12]}",
            run_id=data["run_id"],
            gate=data["gate"],
            status="PENDING",
            created_at=utc_now(),
        )
        session.add(appr)
        session.flush()
        return appr

    @staticmethod
    def decide_approval(
        session: Session,
        approval_id: str,
        status: str,
        decided_by: str,
        reason: str | None = None,
    ) -> ApprovalModel:
        """Ghi nhận quyết định phê duyệt (APPROVED hoặc REJECTED)."""
        appr = session.get(ApprovalModel, approval_id)
        if not appr:
            raise DatabaseRepositoryError(f"Không tìm thấy Approval '{approval_id}'")
        appr.status = status
        appr.decided_by = decided_by
        appr.decided_at = utc_now()
        appr.reason = reason
        session.flush()
        return appr

    @staticmethod
    def record_decision(
        session: Session,
        run_id: str,
        gate: str,
        status: str,
        decided_by: str,
        comment: str | None = None,
    ) -> ApprovalModel:
        """Ghi nhận hoặc tạo mới quyết định phê duyệt theo run_id và gate."""
        approval_id = f"APP-{run_id}-{gate}"
        appr = session.get(ApprovalModel, approval_id)
        now = utc_now()
        if appr is None:
            appr = ApprovalModel(
                approval_id=approval_id,
                run_id=run_id,
                gate=gate,
                status=status,
                decided_by=decided_by,
                decided_at=now,
                reason=comment,
                created_at=now,
            )
            session.add(appr)
        else:
            appr.status = status
            appr.decided_by = decided_by
            appr.decided_at = now
            appr.reason = comment
        session.flush()
        return appr


# =============================================================================
# 7. AUDIT REPOSITORY
# =============================================================================
class AuditRepository:
    """Quản lý ghi nhận Audit Log."""

    @staticmethod
    def record_audit(session: Session, data: dict[str, Any]) -> AuditLogModel:
        """Ghi nhận sự kiện kiểm toán có liên kết mã băm Hash-Chain."""
        import hashlib
        import json

        stmt = select(AuditLogModel.entry_hash).order_by(AuditLogModel.id.desc()).limit(1)
        last_hash = session.execute(stmt).scalar()
        prev_hash = last_hash or ("0" * 64)

        now = data.get("timestamp") or utc_now()
        actor = data.get("actor")
        action = data["action"]
        target = data.get("target")
        result = data["result"]
        details = data.get("details", {})

        from control.app.observability.audit import AuditService
        entry_hash = AuditService.calculate_hash(
            prev_hash=prev_hash,
            actor=actor,
            action=action,
            target=target,
            result=result,
            timestamp=now,
            details=details,
        )

        entry = AuditLogModel(
            run_id=data.get("run_id"),
            task_id=data.get("task_id"),
            agent_id=data.get("agent_id"),
            actor=actor,
            action=action,
            target=target,
            result=result,
            details=details,
            timestamp=now,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        session.add(entry)
        session.flush()
        return entry

    @staticmethod
    def record(
        session: Session,
        action: str,
        result: str,
        actor: str | None = None,
        target: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        agent_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditLogModel:
        """Ghi nhận nhanh sự kiện kiểm toán với các tham số từ khóa."""
        return AuditRepository.record_audit(
            session,
            {
                "actor": actor,
                "action": action,
                "target": target,
                "result": result,
                "task_id": task_id,
                "run_id": run_id,
                "agent_id": agent_id,
                "details": details or {},
            },
        )


# =============================================================================
# 8. LINEAR LINK REPOSITORY
# =============================================================================
class LinearLinkRepository:
    """Quản lý ánh xạ đối tượng nội bộ với Linear."""

    @staticmethod
    def get_link(
        session: Session,
        entity_type: str,
        local_id: str,
    ) -> LinearLinkModel | None:
        """Lấy liên kết Linear theo loại đối tượng và local_id."""
        stmt = select(LinearLinkModel).where(
            LinearLinkModel.entity_type == entity_type,
            LinearLinkModel.local_id == local_id,
        )
        return session.scalars(stmt).first()

    @staticmethod
    def get_by_linear_id(
        session: Session,
        linear_id: str,
    ) -> LinearLinkModel | None:
        """Lấy liên kết Linear theo linear_id."""
        stmt = select(LinearLinkModel).where(
            LinearLinkModel.linear_id == linear_id,
        )
        return session.scalars(stmt).first()

    @staticmethod
    def count_issues_by_prefix(
        session: Session,
        local_id_prefix: str,
    ) -> int:
        """Đếm số lượng issue liên kết thuộc về một run (dựa theo tiền tố local_id hoặc run_id)."""
        stmt = select(func.count(LinearLinkModel.id)).where(
            LinearLinkModel.entity_type == "TASK",
            LinearLinkModel.local_id.like(f"{local_id_prefix}%"),
        )
        return session.scalar(stmt) or 0

    @staticmethod
    def list_by_prefix(
        session: Session,
        local_id_prefix: str,
    ) -> list[LinearLinkModel]:
        """Liệt kê toàn bộ liên kết có local_id bắt đầu bằng tiền tố chỉ định."""
        stmt = select(LinearLinkModel).where(
            LinearLinkModel.local_id.like(f"{local_id_prefix}%"),
        )
        return list(session.scalars(stmt).all())

    @staticmethod
    def upsert_link(
        session: Session,
        entity_type: str,
        local_id: str,
        linear_id: str,
        content_hash: str | None = None,
    ) -> LinearLinkModel:
        """Thêm hoặc cập nhật liên kết Linear."""
        stmt = select(LinearLinkModel).where(
            LinearLinkModel.entity_type == entity_type,
            LinearLinkModel.local_id == local_id,
        )
        link = session.scalars(stmt).first()
        now = utc_now()
        if not link:
            link = LinearLinkModel(
                entity_type=entity_type,
                local_id=local_id,
                linear_id=linear_id,
                content_hash=content_hash,
                last_synced_at=now,
            )
            session.add(link)
        else:
            link.linear_id = linear_id
            link.content_hash = content_hash
            link.last_synced_at = now
        session.flush()
        return link
