"""approval.py - Dịch vụ xử lý phê duyệt tại các cổng kiểm soát (Gates) và duyệt khởi chạy Run."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode
from agent_contract.models import make_error
from agent_contract.system_models import DispatchState, RunStatus, SystemEventType
from control.app.database.models import ApprovalModel, TaskModel, WorkflowRunModel, utc_now
from control.app.database.repositories import (
    ApprovalRepository,
    AuditRepository,
    TaskRepository,
    WorkflowRunRepository,
)
from control.app.queue.events import EventPublisher

logger = logging.getLogger(__name__)


class ApprovalService:
    """Xử lý phê duyệt kế hoạch, chuyển trạng thái Run, và thông qua các cổng kiểm soát GATE."""

    def __init__(self, publisher: Optional[EventPublisher] = None, linear_sync=None) -> None:
        self.publisher = publisher
        self.linear_sync = linear_sync

    def approve(
        self,
        session: Session,
        run_id: str,
        gate: Optional[str] = None,
        decided_by: str = "ADMIN",
        notes: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Phê duyệt Run (từ AWAITING_APPROVAL sang RUNNING) hoặc phê duyệt một GATE cụ thể."""
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        now = utc_now()
        gate_name = gate or "APPROVE_PLAN"

        # 1. Nếu run đang ở trạng thái AWAITING_APPROVAL, chuyển sang RUNNING
        was_awaiting = (run.status == RunStatus.AWAITING_APPROVAL.value)
        if was_awaiting:
            run.status = RunStatus.RUNNING.value
            run.updated_at = now
            logger.info(f"Run '{run_id}' được duyệt, chuyển từ AWAITING_APPROVAL sang RUNNING.")

        # 2. Nếu có gate chỉ định, tìm và hoàn thành task GATE tương ứng
        target_task: Optional[TaskModel] = None
        if gate:
            norm_gate = gate.strip().lower()
            stmt = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state != DispatchState.COMPLETED.value,
            )
            tasks = session.execute(stmt).scalars().all()
            for t in tasks:
                t_act = t.action.lower()
                if (
                    t_act == norm_gate
                    or t_act == f"approve_{norm_gate}"
                    or t.task_id.lower() == norm_gate
                    or norm_gate.endswith(t_act)
                ):
                    target_task = t
                    break

            if target_task:
                target_task.dispatch_state = DispatchState.COMPLETED.value
                target_task.updated_at = now
                target_task.version += 1
                logger.info(f"Cổng kiểm soát '{target_task.task_id}' (action={target_task.action}) được duyệt hoàn tất.")

        # 3. Ghi nhận vào ApprovalRepository
        ApprovalRepository.record_decision(
            session=session,
            run_id=run_id,
            gate=gate_name,
            status="APPROVED",
            decided_by=decided_by,
            comment=notes,
        )

        # 4. Ghi nhận Audit
        AuditRepository.record(
            session=session,
            actor=decided_by,
            action="GATE_APPROVED" if target_task else "RUN_APPROVED",
            target=gate_name,
            run_id=run_id,
            result="APPROVED",
            details={"notes": notes, "was_awaiting": was_awaiting},
        )

        # 5. Phát sự kiện
        if self.publisher:
            self.publisher.publish(
                event_type=SystemEventType.APPROVAL_DECIDED,
                payload={
                    "run_id": run_id,
                    "gate": gate_name,
                    "decision": "APPROVED",
                    "decided_by": decided_by,
                    "run_status": run.status,
                },
                run_id=run_id,
                session=session,
            )

        # 6. Đồng bộ Linear
        if self.linear_sync:
            try:
                self.linear_sync.enqueue_change(
                    entity_type="APPROVAL",
                    local_id=f"gate-{gate_name}",
                    run_id=run_id,
                    data={"gate": gate_name, "status": "APPROVED", "decided_by": decided_by},
                )
            except Exception as e:
                logger.warning(f"Lỗi khi enqueue approval sang Linear: {e}")

        session.commit()
        return {
            "status": "approved",
            "run_id": run_id,
            "gate": gate_name,
            "run_status": run.status,
        }

    def reject(
        self,
        session: Session,
        run_id: str,
        gate: str,
        decided_by: str = "ADMIN",
        reason: str = "Rejected by user",
    ) -> Dict[str, Any]:
        """Từ chối cổng kiểm soát: đánh dấu task FAILED/BLOCKED và dừng run."""
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        now = utc_now()
        norm_gate = gate.strip().lower()

        stmt = select(TaskModel).where(
            TaskModel.run_id == run_id,
            TaskModel.dispatch_state != DispatchState.COMPLETED.value,
        )
        tasks = session.execute(stmt).scalars().all()
        target_task: Optional[TaskModel] = None
        for t in tasks:
            t_act = t.action.lower()
            if (
                t_act == norm_gate
                or t_act == f"approve_{norm_gate}"
                or t.task_id.lower() == norm_gate
                or norm_gate.endswith(t_act)
            ):
                target_task = t
                break

        if target_task:
            target_task.dispatch_state = DispatchState.BLOCKED.value
            target_task.updated_at = now
            target_task.version += 1

        run.status = RunStatus.BLOCKED.value
        run.updated_at = now

        ApprovalRepository.record_decision(
            session=session,
            run_id=run_id,
            gate=gate,
            status="REJECTED",
            decided_by=decided_by,
            comment=reason,
        )

        AuditRepository.record(
            session=session,
            actor=decided_by,
            action="GATE_REJECTED",
            target=gate,
            run_id=run_id,
            result="REJECTED",
            details={"reason": reason},
        )

        if self.publisher:
            self.publisher.publish(
                event_type=SystemEventType.APPROVAL_DECIDED,
                payload={
                    "run_id": run_id,
                    "gate": gate,
                    "decision": "REJECTED",
                    "reason": reason,
                    "run_status": run.status,
                },
                run_id=run_id,
                session=session,
            )

        session.commit()
        return {
            "status": "rejected",
            "run_id": run_id,
            "gate": gate,
            "reason": reason,
            "run_status": run.status,
        }
