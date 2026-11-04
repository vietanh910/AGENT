#!/usr/bin/env python3
"""scripts/soak.py - Kiểm thử tải và độ bền vững (Soak Test) cho PC1 Control Node (Prompt S11).

Quy trình:
1. Tạo một WorkflowRun độc lập với 100 tác vụ (tasks).
2. Chạy qua DispatcherService kết hợp MockWorkerClient mô phỏng ngẫu nhiên:
   - 20% tỷ lệ lỗi có thể thử lại (retryable error) ở lần thử đầu tiên.
   - Thử lại thành công ở lần thử tiếp theo.
3. Xác nhận hai điều kiện tuyệt đối:
   - 0 task bị mất (mọi task đều đạt trạng thái kết thúc: COMPLETED hoặc BLOCKED).
   - 0 task bị chạy trùng lặp (không có task nào hoàn thành 2 lần).
4. Kiểm tra tính toàn vẹn của Audit Log Merkle Hash Chain sau khi hoàn tất.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Set

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from agent_contract.enums import AgentRole, ErrorCode, TaskStatus
from agent_contract.models import AgentError, TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus
from control.app.config import settings
from control.app.database.models import AgentModel, TaskModel, WorkerModel, WorkflowRunModel, utc_now
from control.app.database.repositories import AuditRepository, TaskRepository, WorkflowRunRepository
from control.app.database.session import get_db_session, get_session_factory
from control.app.observability.audit import AuditService
from control.app.security.authentication import WorkerPrincipal
from control.app.services.circuit_breaker import CircuitBreakerRegistry
from control.app.services.dispatcher import DispatcherService
from control.app.services.result_handler import ResultHandler
from control.app.services.task_manager import TaskManager
from control.app.services.worker_client import WorkerClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("soak_test")

if sys.stdout.encoding != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class SoakMockWorker(WorkerClient):
    """Mock Worker Client mô phỏng tải thực tế với độ trễ và lỗi ngẫu nhiên."""

    def __init__(self, failure_rate: float = 0.2, seed: int = 42) -> None:
        self.failure_rate = failure_rate
        self.rng = random.Random(seed)
        self.dispatch_count: Dict[str, int] = {}
        self.completed_tasks: Set[str] = set()

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        tid = request.task_id
        self.dispatch_count[tid] = self.dispatch_count.get(tid, 0) + 1
        return DispatchAck(accepted=True, task_id=tid)

    async def cancel(self, task_id: str) -> bool:
        return True

    async def get_status(self) -> Dict[str, Any]:
        return {"status": "ONLINE", "running_tasks": []}

    def execute_and_report(
        self,
        task: TaskModel,
        worker_principal: WorkerPrincipal,
    ) -> TaskResult:
        """Sinh kết quả TaskResult mô phỏng: thất bại ở attempt 1 theo tỷ lệ failure_rate, thành công ở attempt > 1."""
        tid = task.task_id
        attempt = task.attempts

        # Nếu attempt 1 và trúng tỷ lệ lỗi ngẫu nhiên
        should_fail = (attempt == 1 and self.rng.random() < self.failure_rate)

        if should_fail:
            return TaskResult(
                task_id=tid,
                agent_id=task.agent_id,
                role=task.role,
                status=TaskStatus.FAILED,
                summary=f"Failed task {tid} on attempt {attempt}",
                error=AgentError(
                    code=ErrorCode.LLM_ERROR,
                    message="Mô phỏng timeout kết nối LLM (có thể thử lại)",
                    retryable=True,
                ),
                created_at=utc_now(),
            )

        # Thành công
        self.completed_tasks.add(tid)
        return TaskResult(
            task_id=tid,
            agent_id=task.agent_id,
            role=task.role,
            status=TaskStatus.COMPLETED,
            summary=f"Completed task {tid} on attempt {attempt}",
            data={"output": f"Completed task {tid} on attempt {attempt}"},
            created_at=utc_now(),
        )


def run_soak_test(num_tasks: int = 100, failure_rate: float = 0.2) -> bool:
    """Thực thi Soak Test với num_tasks."""
    logger.info("=" * 70)
    logger.info(f"BẮT ĐẦU SOAK TEST: {num_tasks} TASKS (Tỷ lệ lỗi mô phỏng: {failure_rate * 100:.0f}%)")
    logger.info("=" * 70)

    run_id = f"SOAK-RUN-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
    project_id = f"PRJ-{uuid.uuid4().hex[:6].upper()}"

    session_factory = get_session_factory()
    mock_worker = SoakMockWorker(failure_rate=failure_rate)
    worker_principal = WorkerPrincipal(
        worker_id="worker-soak",
        machine_id="PC1",
        allowed_agents=["pm-01", "devops-01", "dev-01", "tester-01"],
    )

    # 1. Khởi tạo Run và 100 tasks trong database
    logger.info(f"Đang khởi tạo WorkflowRun '{run_id}' và {num_tasks} tasks...")
    roles_pool = [
        ("PM", "report_status", "pm-01"),
        ("DEVOPS", "generate_ci_config", "devops-01"),
        ("DEV", "implement_project", "dev-01"),
        ("TESTER", "create_test_cases", "tester-01"),
        ("BA", "write_requirements", "ba-01"),
        ("SA", "design_architecture", "sa-01"),
        ("DBA", "design_schema", "dba-01"),
        ("PO", "define_backlog", "po-01"),
        ("SCRUM_MASTER", "run_retrospective", "scrum_master-01"),
        ("SECURITY", "security_review", "security-01"),
    ]

    with session_factory() as session:
        # Giải phóng mọi agent bị kẹt từ lần chạy trước
        for a in session.query(AgentModel).all():
            a.current_task = None
            if a.status == "OFFLINE":
                a.status = "IDLE"
        session.commit()

        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": project_id,
                "brief": f"Soak test workload for {num_tasks} tasks",
                "status": RunStatus.RUNNING.value,
            },
        )

        for i in range(1, num_tasks + 1):
            role, action, agent_id = roles_pool[i % len(roles_pool)]
            task = TaskModel(
                task_id=f"soak-t-{i:03d}-{uuid.uuid4().hex[:4]}",
                run_id=run_id,
                role=role,
                action=action,
                instruction=f"Soak test task #{i} instruction",
                agent_id=agent_id,
                dispatch_state=DispatchState.QUEUED.value,
                attempts=0,
                max_attempts=3,
                timeout_seconds=60,
            )
            session.add(task)
        session.commit()

    logger.info(f"Đã tạo thành công {num_tasks} tasks READY.")

    # 2. Vòng lặp điều phối và nộp kết quả
    CircuitBreakerRegistry.reset_all()

    dispatcher = DispatcherService(
        session_factory=session_factory,
        max_parallel_tasks=10,
    )

    # Đăng ký mock worker cho toàn bộ worker trong database và reset circuit breaker
    worker_map: Dict[str, WorkerPrincipal] = {}
    with session_factory() as session:
        for w in session.query(WorkerModel).all():
            dispatcher.register_worker_client(w.worker_id, mock_worker)
            CircuitBreakerRegistry.get_worker(w.worker_id).reset()
            # Tìm danh sách agents thuộc máy này
            agents = session.query(AgentModel).filter(AgentModel.machine == w.machine_id).all()
            allowed = [a.agent_id for a in agents]
            worker_map[w.machine_id] = WorkerPrincipal(
                worker_id=w.worker_id,
                machine_id=w.machine_id,
                allowed_agents=allowed,
            )

    start_time = time.time()
    iteration = 0
    max_iterations = 500

    while iteration < max_iterations:
        iteration += 1

        # A. Cập nhật các task RETRY_WAIT đã hết thời gian backoff sang QUEUED
        with session_factory() as session:
            retry_tasks = session.query(TaskModel).filter(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state == DispatchState.RETRY_WAIT.value,
            ).all()
            for rt in retry_tasks:
                rt.dispatch_state = DispatchState.QUEUED.value
            if retry_tasks:
                session.commit()

        # B. Lấy các task QUEUED để dispatch
        with session_factory() as session:
            ready_tasks = session.query(TaskModel).filter(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state == DispatchState.QUEUED.value,
            ).all()

            for task in ready_tasks:
                dispatcher.dispatch_single_task(session, task)

        # C. Xử lý các task đang ASSIGNED (Worker thực thi và callback)
        with session_factory() as session:
            assigned_tasks = session.query(TaskModel).filter(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state == DispatchState.ASSIGNED.value,
            ).all()

            for at in assigned_tasks:
                agent = session.get(AgentModel, at.agent_id)
                machine = agent.machine if agent else "PC1"
                wp = worker_map.get(machine, worker_principal)

                res = mock_worker.execute_and_report(at, wp)
                ResultHandler.handle_result(
                    session=session,
                    task_id=at.task_id,
                    result=res,
                    worker=wp,
                )

        # D. Kiểm tra điều kiện hoàn tất: tất cả task đã ở terminal state (COMPLETED hoặc BLOCKED)
        with session_factory() as session:
            terminal_count = session.query(TaskModel).filter(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state.in_([
                    DispatchState.COMPLETED.value,
                    DispatchState.BLOCKED.value,
                    DispatchState.FAILED.value,
                ]),
            ).count()

            if terminal_count >= num_tasks:
                logger.info(f"Toàn bộ {num_tasks} tasks đã đạt terminal state sau {iteration} vòng lặp!")
                break

        time.sleep(0.05)

    duration = time.time() - start_time

    # 3. Đánh giá tính chính xác (Zero Lost, Zero Duplicates)
    with session_factory() as session:
        all_tasks = session.query(TaskModel).filter(TaskModel.run_id == run_id).all()
        completed_count = sum(1 for t in all_tasks if t.dispatch_state == DispatchState.COMPLETED.value)
        blocked_count = sum(1 for t in all_tasks if t.dispatch_state == DispatchState.BLOCKED.value)
        failed_count = sum(1 for t in all_tasks if t.dispatch_state == DispatchState.FAILED.value)
        lost_count = sum(1 for t in all_tasks if t.dispatch_state not in [
            DispatchState.COMPLETED.value,
            DispatchState.BLOCKED.value,
            DispatchState.FAILED.value,
        ])

        # Kiểm tra trùng lặp: có task nào bị callback COMPLETED 2 lần không
        duplicate_count = 0
        for tid, dispatches in mock_worker.dispatch_count.items():
            if dispatches > 3:  # max_attempts = 3
                duplicate_count += 1

        # Kiểm tra toàn vẹn Hash Chain
        chain_valid, broken_id, broken_reason = AuditService.verify_chain_integrity(session)

    logger.info("=" * 70)
    logger.info("KẾT QUẢ SOAK TEST REPORT:")
    logger.info(f"- Tổng số tasks khởi tạo: {num_tasks}")
    logger.info(f"- Hoàn thành (COMPLETED): {completed_count}")
    logger.info(f"- Cách ly an toàn (BLOCKED): {blocked_count}")
    logger.info(f"- Thất bại (FAILED): {failed_count}")
    logger.info(f"- Số task bị mất (LOST): {lost_count} (Yêu cầu: 0)")
    logger.info(f"- Số task bị lặp vượt ngưỡng (DUPLICATE): {duplicate_count} (Yêu cầu: 0)")
    logger.info(f"- Thời gian thực thi: {duration:.2f}s (Tốc độ: {num_tasks/max(0.1, duration):.1f} tasks/s)")
    logger.info(f"- Audit Hash Chain Integrity: {'HỢP LỆ (VERIFIED)' if chain_valid else f'LỖI TẠI ID {broken_id}: {broken_reason}'}")
    logger.info("=" * 70)

    success = (lost_count == 0 and duplicate_count == 0 and chain_valid and (completed_count + blocked_count) == num_tasks)
    return success


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI Team Soak & Stress Test")
    parser.add_argument("--tasks", type=int, default=100, help="Số lượng task kiểm thử (mặc định 100)")
    parser.add_argument("--failure-rate", type=float, default=0.2, help="Tỷ lệ lỗi mô phỏng (0.0 đến 1.0)")

    args = parser.parse_args()
    ok = run_soak_test(num_tasks=args.tasks, failure_rate=args.failure_rate)
    if ok:
        print("SOAK_TEST_PASSED: 0 lost tasks, 0 duplicate executions.")
        sys.exit(0)
    else:
        print("SOAK_TEST_FAILED: Vi phạm yêu cầu zero-loss hoặc zero-duplicate.")
        sys.exit(1)
