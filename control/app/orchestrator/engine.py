"""engine.py - Động cơ điều hành thực thi Orchestrator Engine trên PC1 Control Node (Prompt S9).

Trách nhiệm:
- Điều khiển vòng đời thực thi WorkflowRun qua LangGraph.
- Điều khiển luồng: cancel (gửi cancel tới worker), pause, resume, unblock.
- Khôi phục sau sự cố (Crash Recovery): dựng lại hoàn toàn từ bảng MySQL khi PC1 khởi động lại.
  + Task ASSIGNED chưa có xác nhận -> giao lại (idempotent, cùng task_id).
  + Task RUNNING -> chờ heartbeat hoặc kết quả.
- Đảm bảo tính cô lập giữa các run và chia lượt công bằng (Fair Scheduling).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode
from agent_contract.models import TaskRequest
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus, SystemEventType
from control.app.config import settings
from control.app.database.models import (
    ApprovalModel,
    TaskModel,
    WorkerModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import (
    ApprovalRepository,
    AuditRepository,
    TaskRepository,
    WorkflowRunRepository,
)
from control.app.database.session import get_session_factory
from control.app.orchestrator.checkpoint import CheckpointStore, get_checkpoint_store
from control.app.orchestrator.graph import build_orchestrator_graph
from control.app.orchestrator.guards import WorkflowGuards
from control.app.orchestrator.scheduler import SchedulerService
from control.app.orchestrator.state import WorkflowState
from control.app.queue.events import EventPublisher, get_event_publisher
from control.app.queue.redis import get_redis_client
from control.app.services.dispatcher import DispatcherService
from control.app.services.plan_service import PlanService
from control.app.services.worker_client import WorkerClient

logger = logging.getLogger(__name__)


class OrchestratorEngine:
    """Động cơ trung tâm quản lý và thực thi luồng Workflow của PC1 Control Node."""

    def __init__(
        self,
        session_factory=None,
        checkpoint_store: Optional[CheckpointStore] = None,
        publisher: Optional[EventPublisher] = None,
        dispatcher: Optional[DispatcherService] = None,
    ) -> None:
        self.session_factory = session_factory or get_session_factory()
        self.checkpoint_store = checkpoint_store or get_checkpoint_store(mode="null")
        self.publisher = publisher or get_event_publisher()
        self.guards = WorkflowGuards()
        self.scheduler = SchedulerService()

        # Khởi tạo dispatcher nếu chưa truyền vào
        if dispatcher is not None:
            self.dispatcher = dispatcher
        else:
            try:
                redis_c = get_redis_client()
            except Exception:
                redis_c = None
            self.dispatcher = DispatcherService(
                session_factory=self.session_factory,
                redis_client=redis_c,
                publisher=self.publisher,
            )

        # Xây dựng LangGraph StateGraph
        checkpointer = self.checkpoint_store.get_checkpointer()
        self.graph = build_orchestrator_graph(checkpointer=checkpointer)

    # -------------------------------------------------------------------------
    # ĐIỀU KHIỂN RUN (START, STEP, RUN_LOOP)
    # -------------------------------------------------------------------------
    def start_run(
        self,
        prompt: str,
        project_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        """Khởi tạo một WorkflowRun mới và bắt đầu điều phối."""
        rid = run_id or f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        pid = project_id or f"PRJ-{uuid.uuid4().hex[:6].upper()}"

        with self.session_factory() as session:
            run = WorkflowRunRepository.create_run(
                session=session,
                run_data={
                    "run_id": rid,
                    "project_id": pid,
                    "brief": prompt,
                    "status": RunStatus.CREATED.value,
                },
            )
            session.commit()

        logger.info(f"Đã khởi tạo Run '{rid}' (project={pid}).")
        return rid

    def step_run(self, run_id: str) -> WorkflowState:
        """Thực thi một bước / chu kỳ điều phối của WorkflowRun qua LangGraph."""
        # 1. Tải trạng thái hiện tại từ DB
        with self.session_factory() as session:
            run = session.get(WorkflowRunModel, run_id)
            if not run:
                raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

            meta = run.run_metadata or {}
            initial_state: WorkflowState = {
                "run_id": run_id,
                "project_id": run.project_id,
                "brief": run.brief,
                "fix_loop_count": meta.get("fix_loop_count", 0),
                "stop_requested": meta.get("stop_requested", False),
                "is_paused": (run.status == "PAUSED"),
                "current_stage": run.current_stage or "init",
                "last_processed_task_id": meta.get("last_processed_task_id"),
                "processed_task_ids": list(meta.get("processed_task_ids", [])),
            }

        # 2. Chạy một chu kỳ qua StateGraph
        config = {
            "configurable": {
                "thread_id": run_id,
                "dispatcher": self.dispatcher,
            }
        }
        try:
            result_state = self.graph.invoke(initial_state, config=config)
        except Exception as exc:
            logger.exception(f"Lỗi khi thực thi graph cho run '{run_id}': {exc}")
            result_state = dict(initial_state)
            result_state["error_message"] = str(exc)
            result_state["is_failed"] = True

        # 3. Đồng bộ lại metadata vào DB
        with self.session_factory() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run:
                m = dict(run.run_metadata or {})
                m["fix_loop_count"] = result_state.get("fix_loop_count", 0)
                m["stop_requested"] = result_state.get("stop_requested", False)
                if result_state.get("last_processed_task_id"):
                    m["last_processed_task_id"] = result_state.get("last_processed_task_id")
                if result_state.get("processed_task_ids"):
                    m["processed_task_ids"] = result_state.get("processed_task_ids")
                run.run_metadata = m
                session.commit()

        return result_state

    def run_until_stable(self, run_id: str, max_cycles: int = 50) -> WorkflowState:
        """Thực thi các chu kỳ điều phối cho tới khi run đạt trạng thái ổn định (Awaiting, Blocked, Done, Pause, Fail)."""
        cycle = 0
        final_state: Optional[WorkflowState] = None

        while cycle < max_cycles:
            cycle += 1
            state = self.step_run(run_id)
            final_state = state

            # Kiểm tra trạng thái run trong DB
            with self.session_factory() as session:
                run = session.get(WorkflowRunModel, run_id)
                if not run:
                    break
                status = run.status

            if status in (
                RunStatus.COMPLETED.value,
                RunStatus.FAILED.value,
                RunStatus.CANCELLED.value,
                RunStatus.BLOCKED.value,
                "PAUSED",
            ):
                logger.info(f"Run '{run_id}' đạt trạng thái dừng ({status}) sau {cycle} chu kỳ.")
                break

            if status == RunStatus.AWAITING_APPROVAL.value:
                logger.info(f"Run '{run_id}' dừng chờ duyệt (AWAITING_APPROVAL).")
                break

            # Nếu không còn task ready và không có task đang active
            inspect = state.get("active_tasks_count", 0)
            ready = state.get("ready_tasks_count", 0)
            if inspect == 0 and ready == 0 and status == RunStatus.RUNNING.value:
                # Toàn bộ task có thể đã xong
                if state.get("is_completed"):
                    break

        return final_state or {}

    # -------------------------------------------------------------------------
    # ĐIỀU KHIỂN RUN (CANCEL, PAUSE, RESUME, UNBLOCK)
    # -------------------------------------------------------------------------
    def cancel_run(
        self,
        run_id: str,
        actor: str = "ADMIN",
        reason: str = "User requested cancellation",
        session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """Hủy bỏ WorkflowRun: gửi lệnh hủy tới các Worker, cập nhật trạng thái CANCELLED, ghi audit log."""
        def _do_cancel(sess: Session) -> Dict[str, Any]:
            run = sess.get(WorkflowRunModel, run_id)
            if not run:
                raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

            # 1. Hủy các task đang ASSIGNED hoặc RUNNING
            stmt = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state.in_([
                    DispatchState.ASSIGNED.value,
                    DispatchState.RUNNING.value,
                    DispatchState.QUEUED.value,
                    DispatchState.RETRY_WAIT.value,
                ]),
            )
            active_tasks = sess.execute(stmt).scalars().all()
            now = utc_now()

            for t in active_tasks:
                t.dispatch_state = DispatchState.CANCELLED.value
                t.updated_at = now
                t.version += 1

            run.status = RunStatus.CANCELLED.value
            run.updated_at = now
            m = dict(run.run_metadata or {})
            m["stop_requested"] = True
            run.run_metadata = m

            AuditRepository.record(
                session=sess,
                actor=actor,
                action="RUN_CANCELLED",
                target=run_id,
                run_id=run_id,
                result="CANCELLED",
                details={"reason": reason, "cancelled_tasks": len(active_tasks)},
            )

            if self.publisher:
                self.publisher.publish(
                    event_type=SystemEventType.RUN_FAILED,
                    payload={"run_id": run_id, "status": "CANCELLED", "reason": reason},
                    run_id=run_id,
                    session=sess,
                )

            sess.commit()
            logger.info(f"Đã hủy WorkflowRun '{run_id}' và {len(active_tasks)} tasks liên quan.")
            return {"status": "cancelled", "run_id": run_id, "cancelled_tasks": len(active_tasks)}

        if session is not None:
            return _do_cancel(session)
        with self.session_factory() as sess:
            return _do_cancel(sess)

    def pause_run(
        self,
        run_id: str,
        actor: str = "ADMIN",
        session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """Tạm dừng WorkflowRun: không giao thêm task mới, giữ nguyên trạng thái các task đang chạy."""
        def _do_pause(sess: Session) -> Dict[str, Any]:
            run = sess.get(WorkflowRunModel, run_id)
            if not run:
                raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

            run.status = "PAUSED"
            run.updated_at = utc_now()

            AuditRepository.record(
                session=sess,
                actor=actor,
                action="RUN_PAUSED",
                target=run_id,
                run_id=run_id,
                result="PAUSED",
            )
            sess.commit()
            logger.info(f"Đã tạm dừng WorkflowRun '{run_id}'.")
            return {"status": "paused", "run_id": run_id}

        if session is not None:
            return _do_pause(session)
        with self.session_factory() as sess:
            return _do_pause(sess)

    def resume_run(
        self,
        run_id: str,
        actor: str = "ADMIN",
        session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """Tiếp tục chạy lại WorkflowRun bị tạm dừng."""
        def _do_resume(sess: Session) -> Dict[str, Any]:
            run = sess.get(WorkflowRunModel, run_id)
            if not run:
                raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

            run.status = RunStatus.RUNNING.value
            run.updated_at = utc_now()

            AuditRepository.record(
                session=sess,
                actor=actor,
                action="RUN_RESUMED",
                target=run_id,
                run_id=run_id,
                result="RESUMED",
            )
            sess.commit()
            logger.info(f"Đã tiếp tục chạy lại WorkflowRun '{run_id}'.")
            return {"status": "resumed", "run_id": run_id}

        if session is not None:
            return _do_resume(session)
        with self.session_factory() as sess:
            return _do_resume(sess)

    def unblock_task(
        self,
        run_id: str,
        task_id: str,
        additional_context: Dict[str, Any],
        actor: str = "ADMIN",
        comment: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> Dict[str, Any]:
        """Mở khóa (unblock) task bị kẹt do thiếu ngữ cảnh (MISSING_CONTEXT):

        - Cập nhật context bổ sung.
        - Chuyển dispatch_state của task về QUEUED.
        - Chuyển trạng thái run về RUNNING để tiếp tục điều phối.
        """
        def _do_unblock(sess: Session) -> Dict[str, Any]:
            task = sess.get(TaskModel, task_id)
            if not task or task.run_id != run_id:
                raise ValueError(f"Không tìm thấy task '{task_id}' trong run '{run_id}'.")

            ctx = dict(task.context or {})
            ctx.update(additional_context)
            task.context = ctx
            task.dispatch_state = DispatchState.QUEUED.value
            task.updated_at = utc_now()
            task.version += 1

            run = sess.get(WorkflowRunModel, run_id)
            if run and run.status == RunStatus.BLOCKED.value:
                run.status = RunStatus.RUNNING.value
                run.updated_at = utc_now()

            AuditRepository.record(
                session=sess,
                actor=actor,
                action="TASK_UNBLOCKED",
                target=task_id,
                run_id=run_id,
                result="QUEUED",
                details={"additional_context_keys": list(additional_context.keys()), "comment": comment},
            )

            if self.publisher:
                self.publisher.publish(
                    event_type=SystemEventType.TASK_DISPATCHED,
                    payload={"task_id": task_id, "run_id": run_id, "status": "QUEUED", "unblocked": True},
                    run_id=run_id,
                    task_id=task_id,
                    session=sess,
                )

            sess.commit()
            logger.info(f"Đã giải phóng task '{task_id}' và đưa về hàng đợi QUEUED.")
            return {"status": "unblocked", "task_id": task_id, "run_id": run_id, "dispatch_state": task.dispatch_state}

        if session is not None:
            return _do_unblock(session)
        with self.session_factory() as sess:
            return _do_unblock(sess)

    # -------------------------------------------------------------------------
    # KHÔI PHỤC SAU SỰ CỐ (CRASH RECOVERY)
    # -------------------------------------------------------------------------
    def recover_system(self) -> Dict[str, Any]:
        """Quy trình khôi phục hệ thống khi PC1 Control Node khởi động lại (Prompt S9 - Mục 7):

        1. Quét tìm tất cả các run có trạng thái RUNNING, AWAITING_APPROVAL, BLOCKED, PAUSED.
        2. Dựng lại trạng thái điều phối hoàn toàn từ cơ sở dữ liệu MySQL (không phụ thuộc checkpointer).
        3. Với task ASSIGNED chưa nhận được xác nhận từ Worker: chuyển lại QUEUED hoặc giao lại (idempotent, cùng task_id).
        4. Với task RUNNING: giữ nguyên và chờ heartbeat hoặc callback kết quả từ Worker.
        5. Đảm bảo không mất task và không giao trùng lặp.
        """
        recovered_runs = []
        requeued_tasks = []
        running_tasks_monitored = []

        now = utc_now()
        with self.session_factory() as session:
            # 1. Tìm các run đang dang dở
            stmt_runs = select(WorkflowRunModel).where(
                WorkflowRunModel.status.in_([
                    RunStatus.RUNNING.value,
                    RunStatus.AWAITING_APPROVAL.value,
                    RunStatus.BLOCKED.value,
                    "PAUSED",
                ])
            )
            active_runs = session.execute(stmt_runs).scalars().all()

            for run in active_runs:
                recovered_runs.append(run.run_id)

                # 2. Tìm các task của run này
                stmt_tasks = select(TaskModel).where(TaskModel.run_id == run.run_id)
                tasks = session.execute(stmt_tasks).scalars().all()

                for t in tasks:
                    if t.dispatch_state == DispatchState.ASSIGNED.value:
                        # Task ASSIGNED chưa có xác nhận RUNNING từ Worker -> giao lại (idempotent)
                        # Đưa về QUEUED để lượt schedule tiếp theo dispatch lại cùng task_id
                        t.dispatch_state = DispatchState.QUEUED.value
                        t.updated_at = now
                        t.version += 1
                        requeued_tasks.append(t.task_id)

                    elif t.dispatch_state == DispatchState.RUNNING.value:
                        # Task đang chạy trên Worker -> chờ kết quả hoặc heartbeat
                        running_tasks_monitored.append(t.task_id)

            AuditRepository.record(
                session=session,
                actor="ORCHESTRATOR",
                action="SYSTEM_RECOVERY",
                target="CONTROL_NODE_PC1",
                result="RECOVERED",
                details={
                    "active_runs_count": len(recovered_runs),
                    "requeued_tasks_count": len(requeued_tasks),
                    "running_tasks_monitored_count": len(running_tasks_monitored),
                },
            )
            session.commit()

        logger.info(
            f"Khôi phục hệ thống thành công: {len(recovered_runs)} runs, "
            f"{len(requeued_tasks)} tasks ASSIGNED giao lại, "
            f"{len(running_tasks_monitored)} tasks RUNNING tiếp tục theo dõi."
        )

        return {
            "recovered_runs": recovered_runs,
            "requeued_tasks": requeued_tasks,
            "running_tasks_monitored": running_tasks_monitored,
        }
