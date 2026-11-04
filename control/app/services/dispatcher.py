"""Bộ điều phối tác vụ (Dispatcher) trên PC1 Control Node.

Trách nhiệm:
- Vòng lặp quét và lựa chọn các task READY từ cơ sở dữ liệu.
- Tìm agent tương ứng theo role (v1: mỗi role một agent mặc định).
- Kiểm tra Worker chứa agent phải đang ONLINE và agent chưa bận (tối đa 1 task đồng thời mỗi agent).
- Đảm bảo giới hạn toàn cục số task chạy đồng thời: MAX_PARALLEL_TASKS.
- Lấy khóa phân tán Redis theo task_id (`lock:task:{task_id}`) để tránh dispatch trùng lặp.
- Giao task qua WorkerClient (LocalWorker hoặc RemoteWorker) với DispatchRequest và callback_url:
  + Phản hồi 202: chuyển trạng thái task sang ASSIGNED, ghi nhận attempt, tính deadline.
  + Phản hồi 409 (bận): giữ nguyên QUEUED chờ lượt sau.
  + Phản hồi 422 (sai role/action): chuyển FAILED ngay lập tức, không retry.
  + Lỗi mạng hoặc timeout: chuyển sang RETRY_WAIT với backoff có trần.
- Task chuyển RUNNING khi nhận được heartbeat hoặc sự kiện bắt đầu từ Worker.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, AgentStatus, ErrorCode, Priority
from agent_contract.models import AgentError, Constraints, TaskRequest
from agent_contract.registry import AGENT_REGISTRY_DEFAULT
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus, SystemEventType
from control.app.config import settings
from control.app.database.models import AgentModel, TaskAttemptModel, TaskModel, WorkerModel, WorkflowRunModel, utc_now
from control.app.database.repositories import AuditRepository, TaskRepository
from control.app.queue.events import EventPublisher
from control.app.queue.redis import RedisLockBackend
from control.app.security.authentication import WorkerSecretManager
from control.app.security.permissions import ActionPolicy
from control.app.services.artifact_store import select_input_artifacts
from control.app.services.circuit_breaker import CircuitBreakerRegistry
from control.app.services.task_manager import TaskManager
from control.app.services.worker_client import (
    LocalWorkerClient,
    RemoteWorkerClient,
    WorkerClient,
    WorkerClientError,
)

logger = logging.getLogger(__name__)


class DispatcherService:
    """Bộ điều phối thực thi các tác vụ READY tới các Worker Hosts."""

    def __init__(
        self,
        session_factory: Callable[[], Session],
        redis_client: Optional[redis.Redis] = None,
        publisher: Optional[EventPublisher] = None,
        max_parallel_tasks: int = settings.MAX_PARALLEL_TASKS,
        callback_base_url: Optional[str] = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis_client = redis_client
        self.publisher = publisher
        self.max_parallel_tasks = max_parallel_tasks
        self.callback_base_url = callback_base_url or f"http://{settings.APP_HOST}:{settings.APP_PORT}"
        self._worker_client_registry: Dict[str, WorkerClient] = {}
        self._local_worker = LocalWorkerClient(secret=settings.ADMIN_TOKEN)
        try:
            from agents.devops.agent import DevOpsAgent
            self._local_worker.register_agent_handler("devops-01", DevOpsAgent().handle)
        except Exception as e:
            logger.debug(f"Không thể khởi tạo default DevOpsAgent: {e}")

    def register_worker_client(self, worker_id: str, client: WorkerClient) -> None:
        """Đăng ký custom WorkerClient (dùng cho test hoặc giả lập)."""
        self._worker_client_registry[worker_id] = client

    def get_worker_client(self, session: Session, worker: WorkerModel, agent: AgentModel) -> WorkerClient:
        """Lấy WorkerClient phù hợp cho agent và worker cụ thể."""
        if worker.worker_id in self._worker_client_registry:
            return self._worker_client_registry[worker.worker_id]

        if agent.agent_id in ("pm-01", "devops-01") or worker.machine_id == "PC1":
            return self._local_worker

        # Đọc secret từ WorkerSecretManager
        try:
            from control.app.security.authentication import get_worker_secret_manager
            manager = get_worker_secret_manager()
            w_info = manager.get_worker(worker.worker_id)
            secret = (w_info.get("secret") if w_info else None) or "unknown_worker_secret"
        except Exception:
            secret = "unknown_worker_secret"

        return RemoteWorkerClient(
            base_url=worker.base_url,
            secret=secret,
            timeout=10.0,
        )

    def count_active_tasks(self, session: Session, run_id: Optional[str] = None) -> int:
        """Đếm số lượng task hiện đang chạy (ASSIGNED hoặc RUNNING)."""
        stmt = select(func.count(TaskModel.task_id)).where(
            TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value])
        )
        if run_id:
            stmt = stmt.where(TaskModel.run_id == run_id)
        return session.scalar(stmt) or 0

    def dispatch_single_task(
        self,
        session: Session,
        task: TaskModel,
    ) -> bool:
        """Thực hiện dispatch một task cụ thể tới worker tương ứng.

        Trả về True nếu task được dispatch thành công (ASSIGNED), False nếu bị hoãn hoặc lỗi.
        """
        # 0. Kiểm tra trạng thái Run và kiểm tra task GATE
        run = session.get(WorkflowRunModel, task.run_id)
        if run and run.status == RunStatus.AWAITING_APPROVAL.value:
            logger.debug(f"WorkflowRun '{task.run_id}' đang AWAITING_APPROVAL. Chưa dispatch task '{task.task_id}'.")
            return False
        if run and run.status not in (RunStatus.RUNNING.value, RunStatus.CREATED.value):
            logger.debug(f"WorkflowRun '{task.run_id}' không sẵn sàng (status={run.status}). Bỏ qua task '{task.task_id}'.")
            return False

        if task.role == "GATE" or task.agent_id == "system-gate":
            logger.debug(f"Task '{task.task_id}' là cổng kiểm soát GATE, chờ người dùng phê duyệt.")
            return False

        # Kiểm tra ActionPolicy: vai trò có được phép thực hiện action này không
        if not ActionPolicy.is_action_allowed(task.role, task.action):
            logger.error(f"ActionPolicy vi phạm: Role '{task.role}' không được phép thực hiện action '{task.action}'.")
            task.dispatch_state = DispatchState.FAILED.value
            task.error = AgentError(
                code=ErrorCode.PERMISSION_DENIED,
                message=f"ActionPolicy vi phạm: Role '{task.role}' không có quyền thực hiện action '{task.action}'.",
                retryable=False,
            ).model_dump(mode="json")
            task.updated_at = utc_now()
            task.version += 1
            AuditRepository.record(
                session=session,
                actor="DISPATCHER",
                action="ACTION_POLICY_VIOLATION",
                target=task.task_id,
                run_id=task.run_id,
                result="FAILED",
                details={"role": str(task.role), "action": task.action},
            )
            session.commit()
            return False

        # Kiểm tra điều kiện Deploy nếu task là deploy
        if task.action == "deploy":
            from control.app.services.deploy_guard import DeployGuardService
            ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, task.run_id)
            if not ok:
                logger.warning(f"Chưa đủ điều kiện deploy cho task '{task.task_id}': {reason}")
                return False

        # 1. Kiểm tra giới hạn số task song song
        active_count = self.count_active_tasks(session, run_id=task.run_id)
        if active_count >= self.max_parallel_tasks:
            logger.info(f"Đã đạt giới hạn tối đa song song ({active_count}/{self.max_parallel_tasks}). Tạm hoãn task '{task.task_id}'.")
            return False

        # 2. Xác định Agent
        agent_id = task.agent_id
        agent = session.get(AgentModel, agent_id)
        if not agent:
            logger.warning(f"Không tìm thấy Agent '{agent_id}' cho task '{task.task_id}'.")
            return False

        # 3. Kiểm tra Agent có đang bận không (tối đa 1 task đồng thời mỗi agent)
        if agent.current_task and agent.current_task != task.task_id:
            logger.debug(f"Agent '{agent_id}' đang bận thực hiện task '{agent.current_task}'. Bỏ qua lượt này.")
            return False

        # 4. Kiểm tra Worker chứa Agent phải đang ONLINE
        # Agent map tới worker qua machine_id
        stmt = select(WorkerModel).where(WorkerModel.machine_id == agent.machine)
        worker = session.execute(stmt).scalars().first()
        if not worker or worker.status != "ONLINE":
            logger.info(f"Worker trên máy '{agent.machine}' (cho agent '{agent_id}') đang OFFLINE. Không thể dispatch task '{task.task_id}'.")
            return False

        # Kiểm tra Circuit Breaker của Worker
        worker_cb = CircuitBreakerRegistry.get_worker(worker.worker_id)
        if not worker_cb.can_execute():
            logger.warning(f"CircuitBreaker cho worker '{worker.worker_id}' đang OPEN. Hoãn dispatch task '{task.task_id}'.")
            return False

        # 5. Khóa phân tán Redis lock:task:{task_id}
        lock_key = f"lock:task:{task.task_id}"
        lock_token = None
        if self.redis_client:
            lock_backend = RedisLockBackend(self.redis_client)
            lock_token = lock_backend.acquire(lock_key, ttl_seconds=60)
            if not lock_token:
                logger.debug(f"Không lấy được khóa Redis cho task '{task.task_id}'. Bỏ qua.")
                return False

        try:
            # 6. Chuẩn bị DispatchRequest
            callback_url = f"{self.callback_base_url.rstrip('/')}/api/v1/tasks/{task.task_id}/result"
            attempt_number = task.attempts + 1

            request = DispatchRequest(
                task_id=task.task_id,
                project_id=task.run_id,
                agent_id=task.agent_id,
                role=AgentRole(task.role) if isinstance(task.role, str) else task.role,
                action=task.action,
                instruction=task.instruction,
                context=task.context or {},
                input_artifacts=select_input_artifacts(session, task),
                callback_url=callback_url,
                attempt=attempt_number,
                priority=Priority.MEDIUM,
                constraints=Constraints(timeout_seconds=task.timeout_seconds),
            )

            client = self.get_worker_client(session, worker, agent)
            now = utc_now()

            # Gọi worker dispatch (đồng bộ trong bối cảnh asyncio nếu có hoặc chạy async)
            ack = self._call_worker_dispatch_sync(client, request)

            if ack.accepted:
                # 202 ACCEPTED -> chuyển sang ASSIGNED
                worker_cb.record_success()
                deadline = TaskManager.calculate_deadline(task.timeout_seconds)
                task.dispatch_state = DispatchState.ASSIGNED.value
                task.attempts = attempt_number
                task.deadline = deadline
                task.updated_at = now
                task.version += 1

                agent.current_task = task.task_id
                agent.updated_at = now

                # Ghi nhận TaskAttempt
                TaskManager.record_attempt(
                    session=session,
                    task_id=task.task_id,
                    attempt_number=attempt_number,
                    worker_id=worker.worker_id,
                    status="ASSIGNED",
                )

                AuditRepository.record(
                    session=session,
                    actor="DISPATCHER",
                    action="TASK_DISPATCHED",
                    target=task.task_id,
                    run_id=task.run_id,
                    result="ASSIGNED",
                    details={"worker_id": worker.worker_id, "attempt": attempt_number},
                )

                if self.publisher:
                    self.publisher.publish(
                        event_type=SystemEventType.TASK_DISPATCHED,
                        payload={"task_id": task.task_id, "worker_id": worker.worker_id, "attempt": attempt_number},
                        run_id=task.run_id,
                        task_id=task.task_id,
                        session=session,
                    )

                session.commit()
                return True

            else:
                # Bị từ chối
                if ack.reason == "BUSY":
                    # 409: Agent bận -> giữ QUEUED chờ lượt sau
                    logger.info(f"Worker báo Agent '{agent_id}' đang bận. Giữ nguyên task '{task.task_id}' ở QUEUED.")
                    session.rollback()
                    return False

                elif ack.reason in ("INVALID_ROLE_OR_ACTION", "UNPROCESSABLE"):
                    worker_cb.record_success()
                    # 422: Không hợp lệ -> FAILED ngay lập tức không retry
                    task.dispatch_state = DispatchState.FAILED.value
                    task.error = {
                        "code": ErrorCode.INVALID_INPUT.value,
                        "message": f"Worker từ chối với lý do: {ack.reason}",
                        "retryable": False,
                    }
                    task.updated_at = now
                    task.version += 1

                    AuditRepository.record(
                        session=session,
                        actor="DISPATCHER",
                        action="TASK_DISPATCH_REJECTED",
                        target=task.task_id,
                        run_id=task.run_id,
                        result="FAILED",
                        details={"reason": ack.reason},
                    )
                    session.commit()
                    return False

                else:
                    # Lý do khác
                    session.rollback()
                    return False

        except WorkerClientError as e:
            worker_cb.record_failure()
            logger.warning(f"Lỗi giao tiếp worker khi dispatch task '{task.task_id}': {e.message}")
            # Lỗi mạng hoặc timeout -> chuyển RETRY_WAIT với backoff
            TaskManager.schedule_retry(
                session=session,
                task=task,
                error_details={"code": e.code.value, "message": e.message, "retryable": True},
                worker_id=worker.worker_id if worker else None,
                publisher=self.publisher,
            )
            session.commit()
            return False

        except Exception as e:
            logger.exception(f"Lỗi không mong muốn khi dispatch task '{task.task_id}': {e}")
            session.rollback()
            return False

        finally:
            if self.redis_client and lock_token:
                RedisLockBackend(self.redis_client).release(lock_key, lock_token)

    def _call_worker_dispatch_sync(self, client: WorkerClient, request: DispatchRequest) -> DispatchAck:
        """Bọc lời gọi dispatch async sang đồng bộ một cách an toàn."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, client.dispatch(request))
                    return future.result(timeout=15.0)
            else:
                return loop.run_until_complete(client.dispatch(request))
        except RuntimeError:
            return asyncio.run(client.dispatch(request))

    def run_dispatch_cycle(self, run_id: Optional[str] = None) -> int:
        """Thực hiện một chu kỳ quét và dispatch các task READY.

        Trả về số task đã được dispatch thành công (ASSIGNED).
        """
        dispatched_count = 0
        with self.session_factory() as session:
            ready_tasks = TaskManager.get_ready_tasks(session, run_id=run_id)
            if not ready_tasks:
                return 0

            logger.info(f"Tìm thấy {len(ready_tasks)} task READY.")
            for task in ready_tasks:
                if self.count_active_tasks(session) >= self.max_parallel_tasks:
                    logger.info("Đã đạt giới hạn MAX_PARALLEL_TASKS. Dừng chu kỳ dispatch.")
                    break

                success = self.dispatch_single_task(session, task)
                if success:
                    dispatched_count += 1

        return dispatched_count
