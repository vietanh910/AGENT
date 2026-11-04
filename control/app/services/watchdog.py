"""Watchdog service giám sát hạn chót tác vụ (Deadline) và Worker mất kết nối (Offline).

Chức năng:
1. Quét các task quá deadline:
   - Gửi yêu cầu cancel tới Worker Host.
   - Nếu còn lượt thử: đặt FAILED mã TIMEOUT (retryable=True) -> chuyển sang RETRY_WAIT với backoff.
   - Nếu hết lượt: FAILED cuối cùng.
2. Quét Worker OFFLINE có task đang ASSIGNED / RUNNING:
   - Sau thời hạn ân hạn (grace period, mặc định 30s), chuyển task về RETRY_WAIT để giao lại (cùng task_id).
   - Ghi audit log và phát sự kiện.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_contract.enums import ErrorCode
from agent_contract.system_models import DispatchState, SystemEventType
from control.app.database.models import AgentModel, TaskModel, WorkerModel, utc_now
from control.app.database.repositories import AuditRepository, TaskRepository
from control.app.queue.events import EventPublisher
from control.app.services.task_manager import TaskManager
from control.app.services.worker_client import RemoteWorkerClient, WorkerClient

logger = logging.getLogger(__name__)


class WatchdogService:
    """Service giám sát timeout và worker offline."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        publisher: Optional[EventPublisher] = None,
        grace_period_seconds: int = 30,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher
        self.grace_period_seconds = grace_period_seconds
        self._worker_client_registry: Dict[str, WorkerClient] = {}

    def register_worker_client(self, worker_id: str, client: WorkerClient) -> None:
        """Đăng ký custom client cho watchdog hủy task khi test."""
        self._worker_client_registry[worker_id] = client

    def check_deadlines(self) -> List[str]:
        """Quét và xử lý các task đã vượt quá deadline khi đang ASSIGNED hoặc RUNNING.

        Trả về danh sách task_id đã xử lý timeout.
        """
        now = utc_now()
        timed_out_tasks: List[str] = []

        with self.session_factory() as session:
            stmt = select(TaskModel).where(
                TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value]),
                TaskModel.deadline.is_not(None),
                TaskModel.deadline < now,
            )
            tasks = session.execute(stmt).scalars().all()

            for task in tasks:
                tid = task.task_id
                logger.warning(f"Task '{tid}' đã quá hạn chót deadline ({task.deadline}). Tiến hành hủy và retry.")

                # 1. Thử gửi cancel tới worker nếu có client
                agent = session.get(AgentModel, task.agent_id)
                if agent:
                    stmt_worker = select(WorkerModel).where(WorkerModel.machine_id == agent.machine)
                    worker = session.execute(stmt_worker).scalars().first()
                    if worker and worker.worker_id in self._worker_client_registry:
                        try:
                            client = self._worker_client_registry[worker.worker_id]
                            # Hủy task qua client
                            asyncio.run(client.cancel(tid))
                        except Exception as e:
                            logger.debug(f"Không thể gửi cancel tới worker {worker.worker_id}: {e}")

                # 2. Xử lý timeout với retryable=True
                error_dict = {
                    "code": ErrorCode.TIMEOUT.value,
                    "message": f"Task quá thời hạn thực thi {task.timeout_seconds} giây (Deadline: {task.deadline.isoformat()}).",
                    "retryable": True,
                }

                if task.attempts < task.max_attempts:
                    TaskManager.schedule_retry(
                        session=session,
                        task=task,
                        error_details=error_dict,
                        worker_id=worker.worker_id if worker else None,
                        publisher=self.publisher,
                    )
                else:
                    # Hết lượt thử -> FAILED vĩnh viễn
                    task.dispatch_state = DispatchState.FAILED.value
                    task.error = error_dict
                    task.updated_at = now
                    task.version += 1

                    if agent and agent.current_task == tid:
                        agent.current_task = None
                        if agent.status != "OFFLINE":
                            agent.status = "IDLE"

                    AuditRepository.record(
                        session=session,
                        actor="WATCHDOG",
                        action="TASK_TIMEOUT_FAILED",
                        target=tid,
                        run_id=task.run_id,
                        result="FAILED",
                        details={"attempts": task.attempts, "max_attempts": task.max_attempts},
                    )

                    if self.publisher:
                        self.publisher.publish(
                            event_type=SystemEventType.TASK_FAILED,
                            payload={"task_id": tid, "status": "FAILED", "error": error_dict},
                            run_id=task.run_id,
                            task_id=tid,
                            session=session,
                        )

                timed_out_tasks.append(tid)

            session.commit()

        return timed_out_tasks

    def check_offline_worker_tasks(self) -> List[str]:
        """Quét và thu hồi các task đang chạy trên Worker đã bị OFFLINE quá thời hạn ân hạn.

        Trả về danh sách task_id đã được đặt lại sang RETRY_WAIT.
        """
        now = utc_now()
        recovered_tasks: List[str] = []
        cutoff_time = now - timedelta(seconds=self.grace_period_seconds)

        with self.session_factory() as session:
            # Lấy các worker OFFLINE quá hạn ân hạn
            stmt_workers = select(WorkerModel).where(
                WorkerModel.status == "OFFLINE",
                WorkerModel.updated_at <= cutoff_time,
            )
            offline_workers = session.execute(stmt_workers).scalars().all()

            for worker in offline_workers:
                # Tìm các agent thuộc worker này qua machine_id
                stmt_agents = select(AgentModel).where(AgentModel.machine == worker.machine_id)
                agents = session.execute(stmt_agents).scalars().all()
                agent_ids = [a.agent_id for a in agents]

                if not agent_ids:
                    continue

                # Tìm các task đang ASSIGNED hoặc RUNNING thuộc các agent này
                stmt_tasks = select(TaskModel).where(
                    TaskModel.agent_id.in_(agent_ids),
                    TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value]),
                )
                orphaned_tasks = session.execute(stmt_tasks).scalars().all()

                for task in orphaned_tasks:
                    logger.warning(
                        f"Worker '{worker.worker_id}' OFFLINE quá {self.grace_period_seconds}s. "
                        f"Thu hồi task '{task.task_id}' chuyển sang RETRY_WAIT để giao lại."
                    )

                    error_dict = {
                        "code": ErrorCode.WORKER_OFFLINE.value,
                        "message": f"Worker '{worker.worker_id}' mất kết nối quá thời hạn ân hạn ({self.grace_period_seconds}s).",
                        "retryable": True,
                    }

                    # Chuyển về RETRY_WAIT để giao lại (cùng task_id)
                    task.dispatch_state = DispatchState.RETRY_WAIT.value
                    task.error = error_dict
                    # updated_at = now để task sẵn sàng ngay khi worker online trở lại
                    task.updated_at = now
                    task.version += 1

                    AuditRepository.record(
                        session=session,
                        actor="WATCHDOG",
                        action="TASK_ORPHANED_RESET",
                        target=task.task_id,
                        run_id=task.run_id,
                        result="RETRY_WAIT",
                        details={"worker_id": worker.worker_id, "grace_period": self.grace_period_seconds},
                    )

                    if self.publisher:
                        self.publisher.publish(
                            event_type=SystemEventType.TASK_FAILED,
                            payload={
                                "task_id": task.task_id,
                                "status": "RETRY_WAIT",
                                "reason": f"Worker {worker.worker_id} OFFLINE",
                            },
                            run_id=task.run_id,
                            task_id=task.task_id,
                            session=session,
                        )

                    recovered_tasks.append(task.task_id)

            session.commit()

        return recovered_tasks
