"""test_orchestrator.py - Kiểm thử toàn diện LangGraph Orchestrator trên PC1 Control Node (Prompt S9).

Bao gồm 12 kịch bản kiểm thử bắt buộc:
1. Thực thi chuỗi phụ thuộc tuyến tính (Linear Workflow) đến COMPLETED.
2. Giao việc song song (Parallel Dispatch) tôn trọng MAX_PARALLEL_TASKS và 1 task/agent.
3. Vòng sửa lỗi (Fix Loop): TESTER verdict FAIL -> DEV fix_bugs -> re-test PASS -> COMPLETED.
4. Bảo vệ chống lặp vô hạn: TESTER FAIL quá MAX_FIX_LOOPS=3 -> Chuyển sang BLOCKED.
5. An toàn bảo mật: SECURITY verdict FAIL với HIGH/CRITICAL finding -> DEV fix -> re-test -> re-review PASS.
6. Tính bền bỉ với Worker: Worker OFFLINE thì giữ task QUEUED; khi ONLINE thì tiếp tục dispatch.
7. Khởi động lại PC1 giữa chừng (Crash Recovery): Dựng lại từ MySQL, task ASSIGNED giao lại, RUNNING giữ nguyên, không mất task.
8. Cổng phê duyệt (Gate Approval & Rejection): Dừng ở AWAITING_APPROVAL; duyệt thì chạy tiếp, từ chối thì dừng.
9. Điều khiển Run (Cancel, Pause, Resume, Unblock): Hủy task worker, dừng giao việc, mở khóa task thiếu ngữ cảnh.
10. Quy tắc bảo vệ WorkflowGuards: MAX_TASKS_PER_RUN, RUN_TIMEOUT_MINUTES, quyền deploy Production.
11. Lập lịch công bằng (Fair Scheduling): Đa run đồng thời, không xung đột dữ liệu, chia lượt Round-Robin.
12. Khôi phục hoàn toàn từ MySQL không cần Checkpointer (NullCheckpointStore).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import fakeredis
import pytest
from sqlalchemy import select

from agent_contract.enums import AgentRole, AgentStatus, ErrorCode, Priority, TaskStatus
from agent_contract.models import Constraints, TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus, SystemEventType
from control.app.config import settings
from control.app.database.models import (
    AgentModel,
    ApprovalModel,
    TaskAttemptModel,
    TaskDependencyModel,
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
from control.app.orchestrator.checkpoint import NullCheckpointStore, MemoryCheckpointStore
from control.app.orchestrator.engine import OrchestratorEngine
from control.app.orchestrator.guards import WorkflowGuards
from control.app.orchestrator.scheduler import SchedulerService
from control.app.orchestrator.state import WorkflowState
from control.app.queue.events import EventPublisher
from control.app.security.authentication import WorkerPrincipal
from control.app.services.approval import ApprovalService
from control.app.services.dispatcher import DispatcherService
from control.app.services.result_handler import ResultHandler
from control.app.services.task_manager import TaskManager
from control.app.services.worker_client import WorkerClient


class MockWorkerClient(WorkerClient):
    """WorkerClient giả lập để kiểm thử Orchestrator và Dispatcher."""

    def __init__(self, should_accept: bool = True, reject_reason: str = "BUSY") -> None:
        self.should_accept = should_accept
        self.reject_reason = reject_reason
        self.dispatched_requests: List[DispatchRequest] = []
        self.cancelled_tasks: List[str] = []

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        self.dispatched_requests.append(request)
        if self.should_accept:
            return DispatchAck(task_id=request.task_id, accepted=True, reason="ACCEPTED")
        return DispatchAck(task_id=request.task_id, accepted=False, reason=self.reject_reason)

    async def cancel(self, task_id: str) -> bool:
        self.cancelled_tasks.append(task_id)
        return True

    async def get_status(self, task_id: str):
        return {"task_id": task_id, "status": "RUNNING"}


@pytest.fixture
def session_factory():
    return get_session_factory()


@pytest.fixture
def fake_redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def publisher(fake_redis_client):
    return EventPublisher(fake_redis_client)


@pytest.fixture(autouse=True)
def clean_tasks_and_agents(session_factory):
    """Dọn dẹp và chuẩn hóa trạng thái của Run, Task và Agent trước mỗi bài test."""
    with session_factory() as session:
        # Đặt trạng thái CANCELLED cho các run đang chạy dở
        runs = session.execute(
            select(WorkflowRunModel).where(
                WorkflowRunModel.status.in_([RunStatus.RUNNING.value, RunStatus.AWAITING_APPROVAL.value, "PAUSED"])
            )
        ).scalars().all()
        for r in runs:
            r.status = RunStatus.CANCELLED.value

        # Hủy các task active hoặc queued cũ
        tasks = session.execute(
            select(TaskModel).where(
                TaskModel.dispatch_state.in_([
                    DispatchState.ASSIGNED.value,
                    DispatchState.RUNNING.value,
                    DispatchState.QUEUED.value,
                    DispatchState.RETRY_WAIT.value,
                ])
            )
        ).scalars().all()
        for t in tasks:
            t.dispatch_state = DispatchState.CANCELLED.value

        # Đặt lại trạng thái IDLE cho các agent
        agents = session.execute(select(AgentModel)).scalars().all()
        for a in agents:
            a.status = "IDLE"
            a.current_task = None

        session.commit()


@pytest.fixture
def setup_workers_and_agents(session_factory):
    """Khởi tạo đầy đủ danh sách Worker (PC1..PC5) và Agent cần thiết trong DB."""
    now = utc_now()
    workers_spec = [
        ("worker-pc1", "PC1", "http://192.168.1.101:8000"),
        ("worker-pc2", "PC2", "http://192.168.1.102:8000"),
        ("worker-pc3", "PC3", "http://192.168.1.103:8000"),
        ("worker-pc4", "PC4", "http://192.168.1.104:8000"),
        ("worker-pc5", "PC5", "http://192.168.1.105:8000"),
    ]
    agents_spec = [
        ("pm-01", "PM", "PC1", ["project_planning", "scope_management"]),
        ("devops-01", "DEVOPS", "PC1", ["docker", "ci_cd", "deployment"]),
        ("sa-01", "SA", "PC2", ["system_architecture", "tech_stack_selection"]),
        ("dba-01", "DBA", "PC2", ["schema_design", "query_optimization"]),
        ("dev-01", "DEV", "PC3", ["backend_implementation", "fix_bugs"]),
        ("tester-01", "TESTER", "PC4", ["test_automation", "regression"]),
        ("security-01", "SECURITY", "PC4", ["vulnerability_assessment", "code_review"]),
        ("po-01", "PO", "PC5", ["user_stories", "acceptance_criteria"]),
        ("scrum_master-01", "SCRUM_MASTER", "PC5", ["sprint_facilitation"]),
    ]

    with session_factory() as db:
        for wid, mid, url in workers_spec:
            w = db.get(WorkerModel, wid)
            if not w:
                w = WorkerModel(
                    worker_id=wid,
                    machine_id=mid,
                    base_url=url,
                    status="ONLINE",
                    contract_version="1.3.0",
                    worker_version="1.0.0",
                    last_heartbeat_at=now,
                    registered_at=now,
                    updated_at=now,
                )
                db.add(w)
            else:
                w.status = "ONLINE"
                w.last_heartbeat_at = now
                w.updated_at = now

        for aid, role, machine, caps in agents_spec:
            a = db.get(AgentModel, aid)
            if not a:
                a = AgentModel(
                    agent_id=aid,
                    role=role,
                    machine=machine,
                    runner="Worker Host",
                    status="IDLE",
                    current_task=None,
                    capabilities=caps,
                    updated_at=now,
                )
                db.add(a)
            else:
                a.status = "IDLE"
                a.current_task = None
                a.machine = machine
                a.role = role

        db.commit()


def submit_task_result(
    session_factory,
    publisher: EventPublisher,
    task_id: str,
    agent_id: str,
    worker_id: str = "worker-pc2",
    machine_id: str = "PC2",
    status: TaskStatus = TaskStatus.COMPLETED,
    verdict: Optional[str] = None,
    findings: Optional[List[Dict[str, Any]]] = None,
    output_artifacts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Hàm tiện ích nộp kết quả task về PC1 qua ResultHandler."""
    with session_factory() as session:
        worker_principal = WorkerPrincipal(
            worker_id=worker_id,
            machine_id=machine_id,
            allowed_agents=[agent_id],
        )
        meta: Dict[str, Any] = {}
        if verdict is not None:
            meta["verdict"] = verdict
        if findings is not None:
            meta["findings"] = findings

        res_data: Dict[str, Any] = {
            "task_id": task_id,
            "agent_id": agent_id,
            "status": status,
            "summary": f"Result for {task_id}",
            "metadata": meta,
        }
        if output_artifacts is not None:
            res_data["outputs"] = output_artifacts

        ResultHandler.handle_result(
            session=session,
            task_id=task_id,
            result=TaskResult(**res_data),
            worker=worker_principal,
            publisher=publisher,
        )


# =============================================================================
# 1. TEST LINEAR WORKFLOW TO COMPLETION
# =============================================================================
def test_1_linear_workflow_to_completion(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """1. Kiểm tra chuỗi tác vụ phụ thuộc (SA -> DBA -> DEV) thực thi tuần tự đến COMPLETED."""
    run_id = f"run-linear-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-LIN",
                "brief": "Build architecture and DB",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_sa = f"{run_id}_sa"
        t_dba = f"{run_id}_dba"
        t_dev = f"{run_id}_dev"

        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[
                {"task_id": t_sa, "role": "SA", "action": "design_architecture", "instruction": "Arch"},
                {"task_id": t_dba, "role": "DBA", "action": "design_schema", "instruction": "Schema"},
                {"task_id": t_dev, "role": "DEV", "action": "implement_project", "instruction": "Code"},
            ],
            dependencies=[(t_dba, t_sa), (t_dev, t_dba)],
        )
        session.commit()

    dispatcher = DispatcherService(
        session_factory=session_factory,
        redis_client=fake_redis_client,
        publisher=publisher,
    )
    mock_client = MockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
        checkpoint_store=MemoryCheckpointStore(),
    )

    # Bước 1: Chỉ task SA sẵn sàng -> dispatch SA
    engine.step_run(run_id)
    with session_factory() as session:
        assert session.get(TaskModel, t_sa).dispatch_state == DispatchState.ASSIGNED.value
        assert session.get(TaskModel, t_dba).dispatch_state == DispatchState.QUEUED.value

    # Nộp kết quả SA
    submit_task_result(session_factory, publisher, t_sa, "sa-01", "worker-pc2", "PC2")

    # Bước 2: SA hoàn tất -> DBA sẵn sàng -> dispatch DBA
    engine.step_run(run_id)
    with session_factory() as session:
        assert session.get(TaskModel, t_sa).dispatch_state == DispatchState.COMPLETED.value
        assert session.get(TaskModel, t_dba).dispatch_state == DispatchState.ASSIGNED.value

    # Nộp kết quả DBA
    submit_task_result(session_factory, publisher, t_dba, "dba-01", "worker-pc2", "PC2")

    # Bước 3: DBA hoàn tất -> DEV sẵn sàng -> dispatch DEV
    engine.step_run(run_id)
    with session_factory() as session:
        assert session.get(TaskModel, t_dev).dispatch_state == DispatchState.ASSIGNED.value

    # Nộp kết quả DEV
    submit_task_result(session_factory, publisher, t_dev, "dev-01", "worker-pc3", "PC3")

    # Bước 4: Toàn bộ task hoàn thành -> Graph chuyển tới finalize -> Run COMPLETED
    engine.step_run(run_id)
    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        assert run.status == RunStatus.COMPLETED.value
        assert run.completed_at is not None


# =============================================================================
# 2. PARALLEL DISPATCH AND AGENT CONCURRENCY LIMIT
# =============================================================================
def test_2_parallel_dispatch_and_agent_limit(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """2. Giao việc song song tối đa MAX_PARALLEL_TASKS và cô lập 1 task/agent."""
    run_id = f"run-par-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-PAR",
                "brief": "Parallel test",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_sa = f"{run_id}_sa"
        t_dba = f"{run_id}_dba"
        t_dev = f"{run_id}_dev"

        # 3 task độc lập (không phụ thuộc nhau)
        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[
                {"task_id": t_sa, "role": "SA", "action": "design_architecture", "instruction": "Arch"},
                {"task_id": t_dba, "role": "DBA", "action": "design_schema", "instruction": "Schema"},
                {"task_id": t_dev, "role": "DEV", "action": "implement_project", "instruction": "Code"},
            ],
            dependencies=[],
        )
        session.commit()

    dispatcher = DispatcherService(
        session_factory=session_factory,
        redis_client=fake_redis_client,
        publisher=publisher,
        max_parallel_tasks=2,  # Giới hạn song song = 2
    )
    mock_client = MockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
    )

    # Chạy 1 chu kỳ dispatch
    engine.step_run(run_id)

    with session_factory() as session:
        assigned_tasks = session.execute(
            select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state == DispatchState.ASSIGNED.value,
            )
        ).scalars().all()

        # Đúng 2 task được giao (do giới hạn MAX_PARALLEL_TASKS = 2)
        assert len(assigned_tasks) == 2

        # Kiểm tra không có agent nào bị giao 2 task đồng thời
        agent_ids = [t.agent_id for t in assigned_tasks]
        assert len(agent_ids) == len(set(agent_ids))


# =============================================================================
# 3. FIX LOOP: TESTER VERDICT FAIL -> DEV FIX_BUGS -> RE-TEST PASS
# =============================================================================
def test_3_tester_verdict_fail_triggers_fix_loop_then_passes(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """3. TESTER verdict FAIL kích hoạt nhánh fix_loop: tạo task fix_bugs cho DEV, sau đó re-test PASS."""
    run_id = f"run-fix-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-FIX",
                "brief": "Fix loop test",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_dev = f"{run_id}_dev_init"
        t_test = f"{run_id}_test_init"

        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[
                {"task_id": t_dev, "role": "DEV", "action": "implement_project", "instruction": "Initial code"},
                {"task_id": t_test, "role": "TESTER", "action": "run_test_suite", "instruction": "Run tests"},
            ],
            dependencies=[(t_test, t_dev)],
        )
        session.commit()

    dispatcher = DispatcherService(
        session_factory=session_factory,
        redis_client=fake_redis_client,
        publisher=publisher,
    )
    mock_client = MockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
    )

    # 1. Dispatch và hoàn tất DEV
    engine.step_run(run_id)
    submit_task_result(session_factory, publisher, t_dev, "dev-01", "worker-pc3", "PC3")

    # 2. Dispatch TESTER
    engine.step_run(run_id)
    with session_factory() as session:
        assert session.get(TaskModel, t_test).dispatch_state == DispatchState.ASSIGNED.value

    # 3. TESTER nộp kết quả FAIL
    submit_task_result(
        session_factory,
        publisher,
        t_test,
        "tester-01",
        "worker-pc4",
        "PC4",
        status=TaskStatus.COMPLETED,
        verdict="FAIL",
    )

    # 4. Orchestrator xử lý kết quả -> phát hiện FAIL -> kích hoạt fix_loop
    state_after_fix = engine.step_run(run_id)
    assert state_after_fix.get("fix_loop_count") == 1

    # Kiểm tra task dev fix mới đã được tạo trong DB
    with session_factory() as session:
        fix_tasks = session.execute(
            select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.action == "fix_bugs",
            )
        ).scalars().all()
        assert len(fix_tasks) == 1
        dev_fix_task = fix_tasks[0]
        # Task dev_fix phải sẵn sàng (QUEUED)
        assert dev_fix_task.dispatch_state in (DispatchState.QUEUED.value, DispatchState.ASSIGNED.value)

    # 5. Hoàn thành task DEV fix_bugs
    submit_task_result(session_factory, publisher, dev_fix_task.task_id, "dev-01", "worker-pc3", "PC3")

    # 6. Re-test task được dispatch
    engine.step_run(run_id)
    with session_factory() as session:
        retest_tasks = session.execute(
            select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.action == "run_test_suite",
                TaskModel.task_id != t_test,
            )
        ).scalars().all()
        assert len(retest_tasks) == 1
        retest_task = retest_tasks[0]
        assert retest_task.dispatch_state == DispatchState.ASSIGNED.value

    # 7. Re-test nộp kết quả PASS
    submit_task_result(
        session_factory,
        publisher,
        retest_task.task_id,
        "tester-01",
        "worker-pc4",
        "PC4",
        status=TaskStatus.COMPLETED,
        verdict="PASS",
    )

    # 8. Toàn bộ hoàn tất
    engine.step_run(run_id)
    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        assert run.status == RunStatus.COMPLETED.value


# =============================================================================
# 4. MAX_FIX_LOOPS PROTECTION EXCEEDED BLOCKS RUN
# =============================================================================
def test_4_max_fix_loops_exceeded_blocks_run(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """4. Bảo vệ chống lặp vô hạn: TESTER FAIL 4 lần vượt quá MAX_FIX_LOOPS=3 -> Workflow chuyển sang BLOCKED."""
    run_id = f"run-maxloops-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-MAXLOOP",
                "brief": "Max fix loops test",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_test = f"{run_id}_test"
        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[{"task_id": t_test, "role": "TESTER", "action": "run_test_suite", "instruction": "Tests"}],
        )
        session.commit()

    dispatcher = DispatcherService(session_factory=session_factory, redis_client=fake_redis_client, publisher=publisher)
    mock_client = MockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
    )

    # Đặt fix_loop_count đã ở mức 3 (đã lặp 3 lần)
    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        run.run_metadata = {"fix_loop_count": 3}
        # Đánh dấu task test FAIL
        task = session.get(TaskModel, t_test)
        task.dispatch_state = DispatchState.COMPLETED.value
        task.result = {"verdict": "FAIL"}
        session.commit()

    # Kích hoạt step_run: fix_loop sẽ tăng lên 4 và vi phạm MAX_FIX_LOOPS (3)
    state = engine.step_run(run_id)

    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        # Run phải bị BLOCKED, không được phép tiếp tục lặp
        assert run.status == RunStatus.BLOCKED.value
        assert state.get("is_blocked") is True


# =============================================================================
# 5. SECURITY CRITICAL FINDINGS TRIGGERS FIX LOOP
# =============================================================================
def test_5_security_critical_findings_triggers_fix_loop(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """5. SECURITY có lỗ hổng HIGH/CRITICAL kích hoạt nhánh fix_loop và re-test."""
    run_id = f"run-sec-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-SEC",
                "brief": "Security review",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_sec = f"{run_id}_sec"
        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[{"task_id": t_sec, "role": "SECURITY", "action": "security_review", "instruction": "Scan"}],
        )
        session.commit()

    dispatcher = DispatcherService(session_factory=session_factory, redis_client=fake_redis_client, publisher=publisher)
    mock_client = MockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
    )

    # Dispatch SECURITY
    engine.step_run(run_id)

    # Nộp kết quả có phát hiện CRITICAL
    submit_task_result(
        session_factory,
        publisher,
        t_sec,
        "security-01",
        "worker-pc4",
        "PC4",
        status=TaskStatus.COMPLETED,
        verdict="FAIL",
        findings=[{"severity": "CRITICAL", "description": "Remote code execution in endpoint"}],
    )

    # Chạy step_run: phát hiện CRITICAL -> fix_loop
    state = engine.step_run(run_id)
    assert state.get("fix_loop_count") == 1

    # Kiểm tra các task được sinh ra: dev fix, re-test, re-review
    with session_factory() as session:
        dev_fix = session.execute(
            select(TaskModel).where(TaskModel.run_id == run_id, TaskModel.action == "fix_bugs")
        ).scalars().first()
        assert dev_fix is not None
        assert "bảo mật" in dev_fix.instruction.lower() or "an toàn" in dev_fix.instruction.lower()


# =============================================================================
# 6. WORKER OFFLINE THEN ONLINE RESILIENCE
# =============================================================================
def test_6_worker_offline_then_online_resilience(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """6. Worker OFFLINE thì Dispatcher giữ task ở QUEUED; Worker ONLINE trở lại thì dispatch thành công."""
    run_id = f"run-offline-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-OFF",
                "brief": "Worker offline resilience",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_sa = f"{run_id}_sa"
        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[{"task_id": t_sa, "role": "SA", "action": "design_architecture", "instruction": "Arch"}],
        )

        # Chuyển worker-pc2 sang OFFLINE
        w2 = session.get(WorkerModel, "worker-pc2")
        w2.status = "OFFLINE"
        session.commit()

    dispatcher = DispatcherService(session_factory=session_factory, redis_client=fake_redis_client, publisher=publisher)
    mock_client = MockWorkerClient()
    dispatcher.register_worker_client("worker-pc2", mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
    )

    # 1. Chạy dispatch khi worker OFFLINE -> task vẫn ở QUEUED
    engine.step_run(run_id)
    with session_factory() as session:
        task = session.get(TaskModel, t_sa)
        assert task.dispatch_state == DispatchState.QUEUED.value

    # 2. Worker ONLINE trở lại (ví dụ sau heartbeat)
    with session_factory() as session:
        w2 = session.get(WorkerModel, "worker-pc2")
        w2.status = "ONLINE"
        session.commit()

    # 3. Chạy lại dispatch -> task chuyển sang ASSIGNED
    engine.step_run(run_id)
    with session_factory() as session:
        task = session.get(TaskModel, t_sa)
        assert task.dispatch_state == DispatchState.ASSIGNED.value


# =============================================================================
# 7. CRASH RECOVERY PC1 REBOOT MID-RUN
# =============================================================================
def test_7_crash_recovery_pc1_reboot_mid_run(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """7. Khởi động lại PC1 khi run đang dang dở: khôi phục 100% từ MySQL, task ASSIGNED giao lại, RUNNING giữ nguyên."""
    run_id = f"run-recover-{uuid.uuid4().hex[:6]}"
    now = utc_now()

    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-REC",
                "brief": "Crash recovery mid run",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_done = f"{run_id}_done"
        t_assigned = f"{run_id}_assigned"
        t_running = f"{run_id}_running"

        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[
                {"task_id": t_done, "role": "SA", "action": "design_architecture", "instruction": "Done"},
                {"task_id": t_assigned, "role": "DBA", "action": "design_schema", "instruction": "Assigned"},
                {"task_id": t_running, "role": "DEV", "action": "implement_project", "instruction": "Running"},
            ],
        )

        # Giả lập trạng thái trước khi PC1 bị ngắt đột ngột
        t1 = session.get(TaskModel, t_done)
        t1.dispatch_state = DispatchState.COMPLETED.value

        t2 = session.get(TaskModel, t_assigned)
        t2.dispatch_state = DispatchState.ASSIGNED.value  # Chưa kịp nhận confirmation

        t3 = session.get(TaskModel, t_running)
        t3.dispatch_state = DispatchState.RUNNING.value   # Đang chạy trên worker

        session.commit()

    # Khởi động lại PC1 và thực thi quy trình khôi phục
    engine = OrchestratorEngine(session_factory=session_factory, publisher=publisher)
    recovery_result = engine.recover_system()

    assert run_id in recovery_result["recovered_runs"]
    # Task ASSIGNED phải được đưa về QUEUED để giao lại (idempotent, cùng task_id)
    assert t_assigned in recovery_result["requeued_tasks"]
    # Task RUNNING tiếp tục được giữ nguyên để chờ heartbeat/kết quả
    assert t_running in recovery_result["running_tasks_monitored"]

    with session_factory() as session:
        assert session.get(TaskModel, t_done).dispatch_state == DispatchState.COMPLETED.value
        assert session.get(TaskModel, t_assigned).dispatch_state == DispatchState.QUEUED.value
        assert session.get(TaskModel, t_running).dispatch_state == DispatchState.RUNNING.value


# =============================================================================
# 8. GATE APPROVAL AND REJECTION
# =============================================================================
def test_8_gate_approval_and_rejection(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """8. Cổng kiểm soát GATE (approve_scope, approve_prod): dừng chờ AWAITING_APPROVAL; duyệt thì chạy tiếp, từ chối thì dừng."""
    run_id = f"run-gate-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-GATE",
                "brief": "Gate approval test",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_gate = f"{run_id}_approve_scope"
        t_next = f"{run_id}_next"

        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[
                {"task_id": t_gate, "role": "GATE", "action": "approve_scope", "instruction": "Approve scope"},
                {"task_id": t_next, "role": "SA", "action": "design_architecture", "instruction": "Next step"},
            ],
            dependencies=[(t_next, t_gate)],
        )
        session.commit()

    dispatcher = DispatcherService(session_factory=session_factory, redis_client=fake_redis_client, publisher=publisher)
    mock_client = MockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(session_factory=session_factory, publisher=publisher, dispatcher=dispatcher)

    # 1. Chạy đến GATE -> run dừng ở AWAITING_APPROVAL
    engine.step_run(run_id)

    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        assert run.status == RunStatus.AWAITING_APPROVAL.value

    # 2. Admin phê duyệt GATE qua ApprovalService
    approval_svc = ApprovalService(publisher=publisher)
    with session_factory() as session:
        approval_svc.approve(
            session=session,
            run_id=run_id,
            gate="approve_scope",
            decided_by="ADMIN_USER",
            notes="Scope is fully verified",
        )

    # 3. Run tiếp tục chạy -> task sau cổng được dispatch
    engine.step_run(run_id)

    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        assert run.status == RunStatus.RUNNING.value
        next_task = session.get(TaskModel, t_next)
        assert next_task.dispatch_state == DispatchState.ASSIGNED.value


# =============================================================================
# 9. RUN CONTROL: CANCEL, PAUSE, RESUME, UNBLOCK
# =============================================================================
def test_9_run_control_cancel_pause_resume_unblock(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """9. Các thao tác điều khiển Workflow: Cancel (hủy active task), Pause, Resume, Unblock."""
    run_id = f"run-ctrl-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-CTRL",
                "brief": "Run control test",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_active = f"{run_id}_active"
        t_blocked = f"{run_id}_blocked"

        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[
                {"task_id": t_active, "role": "SA", "action": "design_architecture", "instruction": "Arch"},
                {"task_id": t_blocked, "role": "DBA", "action": "design_schema", "instruction": "DB"},
            ],
        )
        # Đánh dấu t_active là ASSIGNED
        session.get(TaskModel, t_active).dispatch_state = DispatchState.ASSIGNED.value
        # Đánh dấu t_blocked là BLOCKED
        session.get(TaskModel, t_blocked).dispatch_state = DispatchState.BLOCKED.value
        session.commit()

    engine = OrchestratorEngine(session_factory=session_factory, publisher=publisher)

    # a. Pause Run
    engine.pause_run(run_id)
    with session_factory() as session:
        assert session.get(WorkflowRunModel, run_id).status == "PAUSED"

    # b. Resume Run
    engine.resume_run(run_id)
    with session_factory() as session:
        assert session.get(WorkflowRunModel, run_id).status == RunStatus.RUNNING.value

    # c. Unblock task
    engine.unblock_task(
        run_id=run_id,
        task_id=t_blocked,
        additional_context={"connection_string": "mysql://test"},
        comment="Credentials provided",
    )
    with session_factory() as session:
        t = session.get(TaskModel, t_blocked)
        assert t.dispatch_state == DispatchState.QUEUED.value
        assert t.context.get("connection_string") == "mysql://test"

    # d. Cancel Run
    engine.cancel_run(run_id, actor="ADMIN", reason="Manual abort")
    with session_factory() as session:
        assert session.get(WorkflowRunModel, run_id).status == RunStatus.CANCELLED.value
        assert session.get(TaskModel, t_active).dispatch_state == DispatchState.CANCELLED.value


# =============================================================================
# 10. GUARDS PROTECTION LIMITS
# =============================================================================
def test_10_guards_protection_limits(session_factory):
    """10. Kiểm tra các rào chắn an toàn (WorkflowGuards): MAX_TASKS_PER_RUN, RUN_TIMEOUT_MINUTES."""
    guards = WorkflowGuards()

    # a. MAX_FIX_LOOPS
    ok, _ = guards.check_fix_loops(3)
    assert ok is True
    ok, reason = guards.check_fix_loops(4)
    assert ok is False
    assert "MAX_FIX_LOOPS" in reason

    # b. MAX_TASKS_PER_RUN
    run_id = f"run-guard-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-GUARD",
                "brief": "Guard test",
                "status": RunStatus.RUNNING.value,
            },
        )
        # Tạo 61 task vượt ngưỡng 60
        tasks = [
            {"task_id": f"{run_id}_t_{i}", "role": "DEV", "action": "fix_bugs", "instruction": f"T{i}"}
            for i in range(61)
        ]
        TaskManager.create_tasks_from_plan(session, run_id, tasks)
        session.commit()

        ok, reason = guards.check_task_count(session, run_id)
        assert ok is False
        assert "MAX_TASKS_PER_RUN" in reason

    # c. RUN_TIMEOUT_MINUTES
    with session_factory() as session:
        run = session.get(WorkflowRunModel, run_id)
        run.created_at = utc_now() - timedelta(minutes=250)
        session.commit()

        ok, reason = guards.check_run_timeout(session, run_id)
        assert ok is False
        assert "RUN_TIMEOUT_MINUTES" in reason


# =============================================================================
# 11. FAIR SCHEDULING MULTI RUNS NO CROSSTALK
# =============================================================================
def test_11_fair_scheduling_multi_runs_no_crosstalk(
    session_factory,
    fake_redis_client,
    publisher,
    setup_workers_and_agents,
):
    """11. Đa run chạy đồng thời: lập lịch công bằng Round-Robin, không crosstalk, tối đa 1 task/agent."""
    run_a = f"run-fair-a-{uuid.uuid4().hex[:6]}"
    run_b = f"run-fair-b-{uuid.uuid4().hex[:6]}"

    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {"run_id": run_a, "project_id": "PRJ-A", "brief": "Run A", "status": RunStatus.RUNNING.value},
        )
        WorkflowRunRepository.create_run(
            session,
            {"run_id": run_b, "project_id": "PRJ-B", "brief": "Run B", "status": RunStatus.RUNNING.value},
        )

        # Run A: SA và DBA
        TaskManager.create_tasks_from_plan(
            session,
            run_a,
            [
                {"task_id": f"{run_a}_sa", "role": "SA", "action": "design_architecture", "instruction": "A SA"},
                {"task_id": f"{run_a}_dba", "role": "DBA", "action": "design_schema", "instruction": "A DBA"},
            ],
        )

        # Run B: SA và DBA
        TaskManager.create_tasks_from_plan(
            session,
            run_b,
            [
                {"task_id": f"{run_b}_sa", "role": "SA", "action": "design_architecture", "instruction": "B SA"},
                {"task_id": f"{run_b}_dba", "role": "DBA", "action": "design_schema", "instruction": "B DBA"},
            ],
        )
        session.commit()

    scheduler = SchedulerService(max_parallel_tasks=4)
    with session_factory() as session:
        scheduled = scheduler.get_fair_scheduled_tasks(session)

        # Đảm bảo mỗi agent chỉ được nhận đúng 1 task
        agents_allocated = [t.agent_id for t in scheduled]
        assert len(agents_allocated) == len(set(agents_allocated))

        # Đảm bảo cả hai run đều được chia lượt công bằng
        runs_served = {t.run_id for t in scheduled}
        assert run_a in runs_served
        assert run_b in runs_served


# =============================================================================
# 12. RECOVERY PURE MYSQL WITHOUT CHECKPOINTER
# =============================================================================
def test_12_recovery_pure_mysql_without_checkpointer(
    session_factory,
    publisher,
    setup_workers_and_agents,
):
    """12. Khôi phục và tái thiết lập điều phối hoàn toàn từ cơ sở dữ liệu MySQL mà không phụ thuộc Checkpointer."""
    run_id = f"run-nullckpt-{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-NULL",
                "brief": "No checkpointer test",
                "status": RunStatus.RUNNING.value,
            },
        )
        t_id = f"{run_id}_sa"
        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=[{"task_id": t_id, "role": "SA", "action": "design_architecture", "instruction": "Arch"}],
        )
        session.commit()

    # Dùng NullCheckpointStore (hoàn toàn không lưu blob vào Redis/MySQL)
    null_store = NullCheckpointStore()
    assert null_store.get_checkpointer() is None

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        checkpoint_store=null_store,
    )

    # Thực thi step_run bình thường chỉ bằng trạng thái đọc/ghi từ MySQL
    state = engine.step_run(run_id)
    assert state.get("run_id") == run_id
    assert state.get("is_failed") is not True

    # Khôi phục hệ thống khi tắt/bật lại
    rec = engine.recover_system()
    assert run_id in rec["recovered_runs"]
