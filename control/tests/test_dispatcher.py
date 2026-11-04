"""Kiểm thử Dispatcher, TaskManager, Watchdog và ResultHandler (Prompt S5).

Bao gồm 10 kịch bản kiểm thử bắt buộc:
1. Giao và nhận kết quả đúng thứ tự phụ thuộc (DAG).
2. Mất callback thì outbox gửi lại và hoàn tất task.
3. Giao trùng task_id không chạy hai lần (Dispatcher idempotency).
4. Callback trùng bị bỏ qua an toàn (ResultHandler idempotency).
5. Worker tắt giữa chừng thì task được giao lại sau thời gian ân hạn.
6. Task quá deadline timeout thì watchdog hủy và retry.
7. Lỗi không retryable (retryable=False) thì chuyển FAILED không retry.
8. Hủy task và hủy toàn bộ run.
9. Khởi động lại PC1 thì task đang chờ không bị mất hay giao trùng.
10. Không giao task cho Worker đang OFFLINE.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

import fakeredis
import httpx
import pytest
from sqlalchemy import select

from agent_contract.enums import AgentRole, AgentStatus, ErrorCode, Priority, TaskStatus
from agent_contract.models import Constraints, TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, SystemEventType
from control.app.config import settings
from control.app.database.models import (
    AgentModel,
    TaskAttemptModel,
    TaskDependencyModel,
    TaskModel,
    WorkerModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import TaskRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.queue.events import EventPublisher
from control.app.security.authentication import WorkerPrincipal
from control.app.services.dispatcher import DispatcherService
from control.app.services.result_handler import ResultHandler
from control.app.services.task_manager import TaskManager
from control.app.services.watchdog import WatchdogService
from control.app.services.worker_client import WorkerClient
from worker.fake_agent import BehaviorConfig, FakeAgent, FakeBehavior
from worker.host.api import create_worker_api
from worker.host.config import WorkerAgentConfig, WorkerHostConfig
from worker.host.outbox import OutboxService
from worker.host.runner import RunnerRegistry


class MockDirectWorkerClient(WorkerClient):
    """WorkerClient gọi trực tiếp tới FastAPI app của Worker qua ASGITransport."""

    def __init__(self, app, secret: str = "worker_pc2_secret_token_change_me") -> None:
        self.app = app
        self.secret = secret
        self.transport = httpx.ASGITransport(app=app)

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        async with httpx.AsyncClient(
            transport=self.transport,
            base_url="http://testworker",
            headers={"Authorization": f"Bearer {self.secret}"},
        ) as client:
            resp = await client.post("/tasks", json=request.model_dump(mode="json"))
            if resp.status_code == 202:
                data = resp.json()
                return DispatchAck(task_id=request.task_id, accepted=True, reason=data.get("reason"))
            if resp.status_code == 409:
                return DispatchAck(task_id=request.task_id, accepted=False, reason="BUSY")
            if resp.status_code == 422:
                return DispatchAck(task_id=request.task_id, accepted=False, reason="INVALID_ROLE_OR_ACTION")
            return DispatchAck(task_id=request.task_id, accepted=False, reason=f"HTTP_{resp.status_code}")

    async def cancel(self, task_id: str) -> bool:
        async with httpx.AsyncClient(
            transport=self.transport,
            base_url="http://testworker",
            headers={"Authorization": f"Bearer {self.secret}"},
        ) as client:
            resp = await client.post(f"/tasks/{task_id}/cancel")
            return resp.status_code == 200

    async def get_status(self, task_id: str):
        async with httpx.AsyncClient(
            transport=self.transport,
            base_url="http://testworker",
            headers={"Authorization": f"Bearer {self.secret}"},
        ) as client:
            resp = await client.get(f"/tasks/{task_id}")
            return resp.json() if resp.status_code == 200 else None


@pytest.fixture
def session_factory():
    return get_session_factory()


@pytest.fixture
def fake_redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def publisher(fake_redis_client):
    return EventPublisher(fake_redis_client)


@pytest.fixture
def setup_db_agents_and_workers(session_factory):
    """Khởi tạo trạng thái ban đầu cho Worker và Agent trong DB."""
    with session_factory() as db:
        # PC2 worker
        w2 = db.get(WorkerModel, "worker-pc2")
        now = utc_now()
        if not w2:
            w2 = WorkerModel(
                worker_id="worker-pc2",
                machine_id="PC2",
                base_url="http://192.168.1.102:8000",
                status="ONLINE",
                last_heartbeat_at=now,
                registered_at=now,
                updated_at=now,
            )
            db.add(w2)
        else:
            w2.status = "ONLINE"
            w2.last_heartbeat_at = now
            w2.updated_at = now

        # sa-01
        sa = db.get(AgentModel, "sa-01")
        if not sa:
            sa = AgentModel(
                agent_id="sa-01",
                role="SA",
                machine="PC2",
                runner="Worker Host",
                status="IDLE",
                current_task=None,
                capabilities=["system_architecture", "tech_stack_selection"],
                updated_at=now,
            )
            db.add(sa)
        else:
            sa.status = "IDLE"
            sa.current_task = None

        # dba-01
        dba = db.get(AgentModel, "dba-01")
        if not dba:
            dba = AgentModel(
                agent_id="dba-01",
                role="DBA",
                machine="PC2",
                runner="Worker Host",
                status="IDLE",
                current_task=None,
                capabilities=["schema_design", "query_optimization"],
                updated_at=now,
            )
            db.add(dba)
        else:
            dba.status = "IDLE"
            dba.current_task = None

        db.commit()


@pytest.fixture
def worker_app_pc2(tmp_path: Path):
    """Tạo Worker Host app cho PC2 dùng database outbox tạm."""
    cfg = WorkerHostConfig(
        worker_id="worker-pc2",
        machine_id="PC2",
        secret="worker_pc2_secret_token_change_me",
        outbox_db_path=str(tmp_path / "w2_outbox.db"),
        agents=[
            WorkerAgentConfig(agent_id="sa-01", role="SA", capabilities=["system_architecture", "tech_stack_selection"]),
            WorkerAgentConfig(agent_id="dba-01", role="DBA", capabilities=["schema_design", "query_optimization"]),
        ],
    )
    outbox = OutboxService(db_path=cfg.outbox_db_path)
    reg = RunnerRegistry(outbox, cfg.secret)
    fake_sa = FakeAgent("sa-01", AgentRole.SA)
    fake_dba = FakeAgent("dba-01", AgentRole.DBA)
    reg.register_runner("sa-01", AgentRole.SA, fake_sa)
    reg.register_runner("dba-01", AgentRole.DBA, fake_dba)
    app = create_worker_api(cfg, reg, outbox)
    app.state.fake_sa = fake_sa
    app.state.fake_dba = fake_dba
    app.state.outbox = outbox
    app.state.runner_registry = reg
    return app


@pytest.fixture(autouse=True)
def clean_active_tasks(session_factory):
    with session_factory() as session:
        tasks = session.execute(
            select(TaskModel).where(
                TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value])
            )
        ).scalars().all()
        for t in tasks:
            t.dispatch_state = DispatchState.CANCELLED.value
        session.commit()


class TestDispatcherAndTaskManager:
    """Kiểm tra toàn bộ luồng điều phối tác vụ và xử lý kết quả."""

    def test_1_dispatch_and_complete_dependencies_in_order(
        self,
        session_factory,
        fake_redis_client,
        publisher,
        setup_db_agents_and_workers,
        worker_app_pc2,
    ):
        """1. Giao và nhận kết quả đúng thứ tự phụ thuộc (Task B phụ thuộc Task A)."""
        run_id = f"run-dep-{uuid.uuid4().hex[:6]}"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-DEP", "brief": "Test DAG"})
            # Task A: SA thiết kế kiến trúc
            # Task B: DBA thiết kế DB (phụ thuộc Task A)
            tasks = TaskManager.create_tasks_from_plan(
                session=session,
                run_id=run_id,
                tasks_data=[
                    {
                        "task_id": f"{run_id}-task-a",
                        "role": "SA",
                        "action": "design_architecture",
                        "instruction": "Design system",
                    },
                    {
                        "task_id": f"{run_id}-task-b",
                        "role": "DBA",
                        "action": "design_schema",
                        "instruction": "Design DB",
                    },
                ],
                dependencies=[(f"{run_id}-task-b", f"{run_id}-task-a")],
            )
            session.commit()

        dispatcher = DispatcherService(
            session_factory=session_factory,
            redis_client=fake_redis_client,
            publisher=publisher,
        )
        mock_client = MockDirectWorkerClient(worker_app_pc2)
        dispatcher.register_worker_client("worker-pc2", mock_client)

        # Chu kỳ 1: Chỉ Task A sẵn sàng (Task B bị chặn do Task A chưa hoàn tất)
        with session_factory() as session:
            ready = TaskManager.get_ready_tasks(session, run_id=run_id)
            assert len(ready) == 1
            assert ready[0].task_id == f"{run_id}-task-a"

        dispatched = dispatcher.run_dispatch_cycle(run_id=run_id)
        assert dispatched == 1

        with session_factory() as session:
            task_a = TaskRepository.get_task(session, f"{run_id}-task-a")
            assert task_a.dispatch_state == DispatchState.ASSIGNED.value

        # Giả lập hoàn thành Task A
        worker_principal = WorkerPrincipal(worker_id="worker-pc2", machine_id="PC2", allowed_agents=["sa-01", "dba-01"])
        with session_factory() as session:
            ResultHandler.handle_result(
                session=session,
                task_id=f"{run_id}-task-a",
                result=TaskResult(task_id=f"{run_id}-task-a", agent_id="sa-01", status=TaskStatus.COMPLETED, summary="Arch done"),
                worker=worker_principal,
                publisher=publisher,
            )

        # Chu kỳ 2: Sau khi Task A hoàn thành -> Task B trở thành READY
        with session_factory() as session:
            ready_after_a = TaskManager.get_ready_tasks(session, run_id=run_id)
            assert len(ready_after_a) == 1
            assert ready_after_a[0].task_id == f"{run_id}-task-b"

        dispatched_b = dispatcher.run_dispatch_cycle(run_id=run_id)
        assert dispatched_b == 1

        with session_factory() as session:
            task_b = TaskRepository.get_task(session, f"{run_id}-task-b")
            assert task_b.dispatch_state == DispatchState.ASSIGNED.value

    def test_2_outbox_resends_lost_callback(
        self,
        session_factory,
        fake_redis_client,
        publisher,
        setup_db_agents_and_workers,
        worker_app_pc2,
    ):
        """2. Mất callback thì outbox lưu lại và gửi lại thành công khi kết nối được."""
        outbox: OutboxService = worker_app_pc2.state.outbox
        task_id = f"task-lost-{uuid.uuid4().hex[:6]}"

        res = TaskResult(task_id=task_id, agent_id="dba-01", status=TaskStatus.COMPLETED, summary="Done")
        outbox.save_result(task_id, "http://control/cb", "sec", res)
        assert outbox.get_pending_count() == 1

        # Lần 1: gửi thất bại
        async def mock_fail(req):
            return httpx.Response(500, json={"error": "fail"})

        success1 = asyncio.run(outbox.flush_task(task_id, client=httpx.AsyncClient(transport=httpx.MockTransport(mock_fail))))
        assert success1 is False
        assert outbox.get_pending_count() == 1

        # Lần 2: gửi thành công
        async def mock_ok(req):
            return httpx.Response(200, json={"status": "accepted"})

        success2 = asyncio.run(outbox.flush_task(task_id, client=httpx.AsyncClient(transport=httpx.MockTransport(mock_ok))))
        assert success2 is True
        assert outbox.get_pending_count() == 0

    def test_3_duplicate_dispatch_does_not_run_twice(
        self,
        session_factory,
        fake_redis_client,
        publisher,
        setup_db_agents_and_workers,
        worker_app_pc2,
    ):
        """3. Giao trùng task_id không chạy hai lần."""
        run_id = f"run-idemp-{uuid.uuid4().hex[:6]}"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-IDEMP", "brief": "Idemp"})
            TaskManager.create_tasks_from_plan(
                session=session,
                run_id=run_id,
                tasks_data=[{"task_id": f"{run_id}-task-1", "role": "SA", "action": "design_architecture", "instruction": "Design"}],
            )
            session.commit()

        dispatcher = DispatcherService(session_factory=session_factory, redis_client=fake_redis_client, publisher=publisher)
        mock_client = MockDirectWorkerClient(worker_app_pc2)
        dispatcher.register_worker_client("worker-pc2", mock_client)

        # Lần 1: Dispatch thành công -> ASSIGNED
        d1 = dispatcher.run_dispatch_cycle(run_id=run_id)
        assert d1 == 1

        # Lần 2: Chạy tiếp dispatch cycle -> Task không còn ở QUEUED/RETRY_WAIT nên không được dispatch lại
        d2 = dispatcher.run_dispatch_cycle(run_id=run_id)
        assert d2 == 0

        with session_factory() as session:
            task = TaskRepository.get_task(session, f"{run_id}-task-1")
            assert task.attempts == 1

    def test_4_duplicate_callback_ignored_safely(
        self,
        session_factory,
        publisher,
        setup_db_agents_and_workers,
    ):
        """4. Callback trùng bị bỏ qua an toàn (Idempotent 200)."""
        run_id = f"run-dup-cb-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-task-1"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-DUP", "brief": "Dup CB"})
            TaskManager.create_tasks_from_plan(
                session=session,
                run_id=run_id,
                tasks_data=[{"task_id": task_id, "role": "SA", "action": "design_architecture", "instruction": "Design"}],
            )
            session.commit()

        worker_principal = WorkerPrincipal(worker_id="worker-pc2", machine_id="PC2", allowed_agents=["sa-01"])
        res = TaskResult(task_id=task_id, agent_id="sa-01", status=TaskStatus.COMPLETED, summary="Finished")

        # Callback lần 1
        with session_factory() as session:
            r1 = ResultHandler.handle_result(session, task_id, res, worker_principal, publisher)
            assert r1["status"] == "accepted"
            assert r1["dispatch_state"] == "COMPLETED"

        # Callback lần 2 trùng lặp -> trả về ignored_duplicate (200)
        with session_factory() as session:
            r2 = ResultHandler.handle_result(session, task_id, res, worker_principal, publisher)
            assert r2["status"] == "ignored_duplicate"

    def test_5_worker_offline_tasks_recovered_after_grace_period(
        self,
        session_factory,
        publisher,
        setup_db_agents_and_workers,
    ):
        """5. Worker tắt giữa chừng thì task được thu hồi về RETRY_WAIT sau thời gian ân hạn."""
        run_id = f"run-offline-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-task-1"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-OFF", "brief": "Offline"})
            t = TaskModel(
                task_id=task_id,
                run_id=run_id,
                agent_id="sa-01",
                role="SA",
                action="design_architecture",
                instruction="Design",
                dispatch_state=DispatchState.RUNNING.value,
                attempts=1,
            )
            session.add(t)

            # Worker PC2 bị OFFLINE từ 40s trước
            w2 = session.get(WorkerModel, "worker-pc2")
            w2.status = "OFFLINE"
            w2.updated_at = utc_now() - timedelta(seconds=40)
            session.commit()

        watchdog = WatchdogService(session_factory=session_factory, publisher=publisher, grace_period_seconds=30)
        recovered = watchdog.check_offline_worker_tasks()
        assert task_id in recovered

        with session_factory() as session:
            t_rec = TaskRepository.get_task(session, task_id)
            assert t_rec.dispatch_state == DispatchState.RETRY_WAIT.value
            assert t_rec.error["code"] == ErrorCode.WORKER_OFFLINE.value

    def test_6_task_deadline_timeout_triggers_cancel_and_retry(
        self,
        session_factory,
        publisher,
        setup_db_agents_and_workers,
    ):
        """6. Task quá deadline thì watchdog hủy và retry."""
        run_id = f"run-timeout-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-task-1"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-TO", "brief": "Timeout"})
            t = TaskModel(
                task_id=task_id,
                run_id=run_id,
                agent_id="sa-01",
                role="SA",
                action="design_architecture",
                instruction="Design",
                dispatch_state=DispatchState.RUNNING.value,
                attempts=1,
                max_attempts=3,
                timeout_seconds=30,
                deadline=utc_now() - timedelta(seconds=5),  # Quá deadline 5s
            )
            session.add(t)
            session.commit()

        watchdog = WatchdogService(session_factory=session_factory, publisher=publisher)
        timed_out = watchdog.check_deadlines()
        assert task_id in timed_out

        with session_factory() as session:
            t_to = TaskRepository.get_task(session, task_id)
            # Còn lượt (1 < 3) nên chuyển sang RETRY_WAIT
            assert t_to.dispatch_state == DispatchState.RETRY_WAIT.value
            assert t_to.error["code"] == ErrorCode.TIMEOUT.value

    def test_7_non_retryable_error_does_not_retry(
        self,
        session_factory,
        publisher,
        setup_db_agents_and_workers,
    ):
        """7. Lỗi không retryable (retryable=False) thì FAILED ngay, không sang RETRY_WAIT."""
        run_id = f"run-nonretry-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-task-1"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-NR", "brief": "Non-retry"})
            t = TaskModel(
                task_id=task_id,
                run_id=run_id,
                agent_id="sa-01",
                role="SA",
                action="design_architecture",
                instruction="Design",
                dispatch_state=DispatchState.RUNNING.value,
                attempts=1,
                max_attempts=3,
            )
            session.add(t)
            session.commit()

        worker_principal = WorkerPrincipal(worker_id="worker-pc2", machine_id="PC2", allowed_agents=["sa-01"])
        res = TaskResult(
            task_id=task_id,
            agent_id="sa-01",
            status=TaskStatus.FAILED,
            summary="Fatal syntax error",
            error={"code": ErrorCode.INVALID_INPUT.value, "message": "Invalid input", "retryable": False},
        )

        with session_factory() as session:
            out = ResultHandler.handle_result(session, task_id, res, worker_principal, publisher)
            assert out["dispatch_state"] == DispatchState.FAILED.value

        with session_factory() as session:
            t_failed = TaskRepository.get_task(session, task_id)
            assert t_failed.dispatch_state == DispatchState.FAILED.value

    def test_8_cancel_task_and_cancel_run(
        self,
        session_factory,
        publisher,
        setup_db_agents_and_workers,
    ):
        """8. Hủy một task cụ thể và hủy toàn bộ workflow run."""
        run_id = f"run-cancel-{uuid.uuid4().hex[:6]}"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-CANCEL", "brief": "Cancel"})
            tasks = TaskManager.create_tasks_from_plan(
                session=session,
                run_id=run_id,
                tasks_data=[
                    {"task_id": f"{run_id}-t1", "role": "SA", "action": "design_architecture", "instruction": "T1"},
                    {"task_id": f"{run_id}-t2", "role": "DBA", "action": "design_schema", "instruction": "T2"},
                ],
            )
            session.commit()

        # Hủy 1 task
        with session_factory() as session:
            cancelled_t = TaskManager.cancel_task(session, f"{run_id}-t1", reason="User cancel single task")
            assert cancelled_t.dispatch_state == DispatchState.CANCELLED.value
            session.commit()

        # Hủy cả run
        with session_factory() as session:
            cancelled_run = TaskManager.cancel_run(session, run_id, reason="User cancel whole run")
            assert cancelled_run.status == "CANCELLED"
            session.commit()

        with session_factory() as session:
            t2 = TaskRepository.get_task(session, f"{run_id}-t2")
            assert t2.dispatch_state == DispatchState.CANCELLED.value

    def test_9_restart_control_tasks_preserved_in_db(
        self,
        session_factory,
        setup_db_agents_and_workers,
    ):
        """9. Khởi động lại PC1 thì task đang chờ (QUEUED) còn nguyên trong DB, không mất hay giao trùng."""
        run_id = f"run-persist-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-task-persist"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-P", "brief": "Persist"})
            TaskManager.create_tasks_from_plan(
                session=session,
                run_id=run_id,
                tasks_data=[{"task_id": task_id, "role": "SA", "action": "design_architecture", "instruction": "Design"}],
            )
            session.commit()

        # Giả lập tắt và mở lại session DB
        with session_factory() as new_session:
            ready = TaskManager.get_ready_tasks(new_session, run_id=run_id)
            assert len(ready) == 1
            assert ready[0].task_id == task_id
            assert ready[0].dispatch_state == DispatchState.QUEUED.value

    def test_10_do_not_dispatch_to_offline_worker(
        self,
        session_factory,
        fake_redis_client,
        publisher,
        setup_db_agents_and_workers,
        worker_app_pc2,
    ):
        """10. Không giao task khi Worker phụ trách đang OFFLINE."""
        run_id = f"run-w-off-{uuid.uuid4().hex[:6]}"
        with session_factory() as session:
            WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-WOFF", "brief": "Worker offline"})
            TaskManager.create_tasks_from_plan(
                session=session,
                run_id=run_id,
                tasks_data=[{"task_id": f"{run_id}-t1", "role": "SA", "action": "design_architecture", "instruction": "Design"}],
            )
            # Đánh dấu Worker PC2 là OFFLINE
            w2 = session.get(WorkerModel, "worker-pc2")
            w2.status = "OFFLINE"
            session.commit()

        dispatcher = DispatcherService(session_factory=session_factory, redis_client=fake_redis_client, publisher=publisher)
        mock_client = MockDirectWorkerClient(worker_app_pc2)
        dispatcher.register_worker_client("worker-pc2", mock_client)

        # Chu kỳ dispatch sẽ bỏ qua không giao cho worker-pc2
        dispatched = dispatcher.run_dispatch_cycle(run_id=run_id)
        assert dispatched == 0

        with session_factory() as session:
            task = TaskRepository.get_task(session, f"{run_id}-t1")
            assert task.dispatch_state == DispatchState.QUEUED.value
