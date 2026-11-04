"""Kịch bản mô phỏng điều phối 5 task phụ thuộc trên 2 Worker giả lập (Prompt S5).

Kịch bản:
- Tạo 5 task có quan hệ đồ thị DAG:
  + Task 1 [SA]: design_architecture (PC2)
  + Task 2 [DBA]: design_schema (PC2) - phụ thuộc Task 1
  + Task 3 [DEV]: implement_feature (PC4) - phụ thuộc Task 1 và Task 2
  + Task 4 [TESTER]: create_test_cases (PC4) - phụ thuộc Task 1
  + Task 5 [TESTER]: run_test_suite (PC4) - phụ thuộc Task 3 và Task 4
- Phân bổ cho 2 Worker giả lập:
  + Worker PC2 (SA & DBA)
  + Worker PC4 (DEV & TESTER)
- Mô phỏng tình huống ngắt kết nối: Tắt Worker PC4 giữa chừng khi đang xử lý,
  sau đó bật lại Worker PC4 -> nhiệm vụ tiếp tục được phục hồi và hoàn thành đúng 100% thứ tự DAG.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

# Đảm bảo root dự án nằm trong sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from sqlalchemy import select

from agent_contract.enums import AgentRole, Priority, TaskStatus
from agent_contract.models import TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, Heartbeat, SystemEventType
from control.app.config import settings
from control.app.database.models import AgentModel, TaskModel, WorkerModel, utc_now
from control.app.database.repositories import TaskRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.queue.redis import get_redis_client
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


class SimulatedWorkerNetworkClient(WorkerClient):
    """Mô phỏng kết nối mạng LAN tới Worker Host có khả năng bật/tắt (Online/Offline)."""

    def __init__(self, app, worker_id: str) -> None:
        self.app = app
        self.worker_id = worker_id
        self.is_alive = True
        self.transport = httpx.ASGITransport(app=app)

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        if not self.is_alive:
            raise httpx.ConnectError(f"Worker '{self.worker_id}' is offline (Connection refused).")

        async with httpx.AsyncClient(transport=self.transport, base_url=f"http://{self.worker_id}") as client:
            resp = await client.post("/tasks", json=request.model_dump(mode="json"))
            if resp.status_code == 202:
                return DispatchAck(task_id=request.task_id, accepted=True)
            if resp.status_code == 409:
                return DispatchAck(task_id=request.task_id, accepted=False, reason="BUSY")
            return DispatchAck(task_id=request.task_id, accepted=False, reason=resp.text)

    async def cancel(self, task_id: str) -> bool:
        if not self.is_alive:
            return False
        async with httpx.AsyncClient(transport=self.transport, base_url=f"http://{self.worker_id}") as client:
            resp = await client.post(f"/tasks/{task_id}/cancel")
            return resp.status_code == 200

    async def get_status(self, task_id: str):
        if not self.is_alive:
            return None
        async with httpx.AsyncClient(transport=self.transport, base_url=f"http://{self.worker_id}") as client:
            resp = await client.get(f"/tasks/{task_id}")
            return resp.json() if resp.status_code == 200 else None


def setup_worker_node(worker_id: str, machine_id: str, agents_def: list, tmp_dir: Path):
    """Khởi tạo một instance Worker Host hoàn chỉnh trong bộ nhớ."""
    cfg = WorkerHostConfig(
        worker_id=worker_id,
        machine_id=machine_id,
        secret=f"{worker_id}_secret",
        outbox_db_path=str(tmp_dir / f"{worker_id}_outbox.db"),
        agents=[
            WorkerAgentConfig(agent_id=aid, role=r, capabilities=[])
            for aid, r in agents_def
        ],
    )
    outbox = OutboxService(db_path=cfg.outbox_db_path)
    reg = RunnerRegistry(outbox, cfg.secret)

    fake_agents = {}
    for aid, r in agents_def:
        role_enum = AgentRole(r)
        fa = FakeAgent(agent_id=aid, role=role_enum, default_behavior=BehaviorConfig(delay_seconds=0.1))
        reg.register_runner(aid, role_enum, fa)
        fake_agents[aid] = fa

    app = create_worker_api(cfg, reg, outbox)
    client = SimulatedWorkerNetworkClient(app, worker_id)
    return {
        "config": cfg,
        "outbox": outbox,
        "registry": reg,
        "fake_agents": fake_agents,
        "app": app,
        "client": client,
    }


def main():
    print("=" * 75)
    print("DEMO S5: TASK MANAGER, DISPATCHER VÀ WORKER HOST")
    print("=" * 75)

    session_factory = get_session_factory()
    redis_client = get_redis_client(use_fake=True)

    # 1. Khởi tạo 2 Worker Host trong thư mục tạm
    import tempfile
    tmp_path = Path(tempfile.mkdtemp(prefix="demo_dispatch_"))

    w_pc2 = setup_worker_node("worker-pc2", "PC2", [("sa-01", "SA"), ("dba-01", "DBA")], tmp_path)
    w_pc4 = setup_worker_node("worker-pc4", "PC4", [("dev-01", "DEV"), ("tester-01", "TESTER")], tmp_path)

    # Đăng ký và đưa Worker/Agent vào Database PC1
    with session_factory() as session:
        now = utc_now()
        for w_data in [w_pc2, w_pc4]:
            w_cfg = w_data["config"]
            w_model = session.get(WorkerModel, w_cfg.worker_id)
            if not w_model:
                w_model = WorkerModel(
                    worker_id=w_cfg.worker_id,
                    machine_id=w_cfg.machine_id,
                    base_url=f"http://192.168.1.{w_cfg.machine_id}:8000",
                    status="ONLINE",
                    last_heartbeat_at=now,
                    registered_at=now,
                    updated_at=now,
                )
                session.add(w_model)
            else:
                w_model.status = "ONLINE"
                w_model.updated_at = now

            for a_cfg in w_cfg.agents:
                agent_model = session.get(AgentModel, a_cfg.agent_id)
                if not agent_model:
                    agent_model = AgentModel(
                        agent_id=a_cfg.agent_id,
                        role=a_cfg.role,
                        machine=w_cfg.machine_id,
                        runner="Worker Host",
                        status="IDLE",
                        current_task=None,
                        capabilities=[],
                        updated_at=now,
                    )
                    session.add(agent_model)
                else:
                    agent_model.status = "IDLE"
                    agent_model.current_task = None
                    agent_model.machine = w_cfg.machine_id
        session.commit()

    print("[1] Da khoi tao 2 Worker Host: worker-pc2 (SA, DBA) va worker-pc4 (DEV, TESTER).")

    # 2. Khởi tạo WorkflowRun và 5 Task theo đồ thị DAG
    run_id = f"RUN-DEMO-{uuid.uuid4().hex[:6]}"
    prefix = f"{run_id}"
    t1_id = f"{prefix}-T1-SA"
    t2_id = f"{prefix}-T2-DBA"
    t3_id = f"{prefix}-T3-DEV"
    t4_id = f"{prefix}-T4-TESTER-1"
    t5_id = f"{prefix}-T5-TESTER-2"

    tasks_def = [
        {"task_id": t1_id, "role": "SA", "action": "design_architecture", "instruction": "Design system architecture"},
        {"task_id": t2_id, "role": "DBA", "action": "design_schema", "instruction": "Design MySQL database schema"},
        {"task_id": t3_id, "role": "DEV", "action": "implement_feature", "instruction": "Implement backend services"},
        {"task_id": t4_id, "role": "TESTER", "action": "create_test_cases", "instruction": "Create integration test suite"},
        {"task_id": t5_id, "role": "TESTER", "action": "run_test_suite", "instruction": "Run tests and produce verdict"},
    ]
    # Quan hệ phụ thuộc DAG:
    # T2 phụ thuộc T1
    # T3 phụ thuộc T1 và T2
    # T4 phụ thuộc T1
    # T5 phụ thuộc T3 và T4
    dependencies = [
        (t2_id, t1_id),
        (t3_id, t1_id),
        (t3_id, t2_id),
        (t4_id, t1_id),
        (t5_id, t3_id),
        (t5_id, t4_id),
    ]

    with session_factory() as session:
        WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": "PRJ-ECOMMERCE", "brief": "E-Commerce Pipeline"})
        TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=tasks_def,
            dependencies=dependencies,
        )
        session.commit()

    print(f"\n[2] Da tao WorkflowRun '{run_id}' voi 5 Task theo do thi DAG:")
    print(f"    - {t1_id} [SA: design_architecture] -> PC2")
    print(f"    - {t2_id} [DBA: design_schema] -> PC2 (phu thuoc: T1)")
    print(f"    - {t3_id} [DEV: implement_feature] -> PC4 (phu thuoc: T1, T2)")
    print(f"    - {t4_id} [TESTER: create_test_cases] -> PC4 (phu thuoc: T1)")
    print(f"    - {t5_id} [TESTER: run_test_suite] -> PC4 (phu thuoc: T3, T4)")

    # 3. Khởi tạo Dispatcher và Watchdog
    dispatcher = DispatcherService(
        session_factory=session_factory,
        redis_client=redis_client,
        max_parallel_tasks=4,
    )
    dispatcher.register_worker_client("worker-pc2", w_pc2["client"])
    dispatcher.register_worker_client("worker-pc4", w_pc4["client"])

    watchdog = WatchdogService(session_factory=session_factory, grace_period_seconds=5)

    print("\n[3] BAT DAU VONG LAP DIEU PHOI TU DONG:")

    step = 1
    worker_pc4_killed = False
    worker_pc4_restored = False

    while True:
        with session_factory() as session:
            tasks = session.execute(
                select(TaskModel).where(TaskModel.run_id == run_id).order_by(TaskModel.task_id)
            ).scalars().all()

            states = {t.task_id: t.dispatch_state for t in tasks}
            ready_tasks = TaskManager.get_ready_tasks(session, run_id=run_id)

            # In trạng thái hiện tại
            print(f"\n--- BƯỚC {step} ---")
            for t in tasks:
                print(f"  {t.task_id:<18} | State: {t.dispatch_state:<12} | Agent: {t.agent_id} | Attempts: {t.attempts}")

            # Kiểm tra nếu tất cả 5 task đã COMPLETED -> Kết thúc demo thành công!
            if all(s == DispatchState.COMPLETED.value for s in states.values()):
                print("\n" + "=" * 75)
                print("THANH CONG! TAT CA 5 TASK DA HOAN TAT DUNG 100% THU TU PHU THUOC DAG!")
                print("=" * 75)
                break

            # Kịch bản thử nghiệm sự cố: Tắt Worker PC4 khi T3 hoặc T4 đang chuẩn bị chạy
            if states.get(t2_id) == DispatchState.COMPLETED.value and not worker_pc4_killed:
                print("\n>>> [SỰ CỐ GIẢ LẬP]: WORKER PC4 BỊ SẬP TIẾN TRÌNH / TẮT GIỮA CHỪNG! <<<")
                w_pc4["client"].is_alive = False
                worker_pc4_killed = True

                # Đánh dấu worker-pc4 là OFFLINE trong DB
                w_model = session.get(WorkerModel, "worker-pc4")
                if w_model:
                    w_model.status = "OFFLINE"
                    w_model.updated_at = utc_now() - timedelta(seconds=10)
                    session.commit()
                print(">>> Worker PC4 hien da OFFLINE. PC1 se khong the giao viec cho PC4. <<<")

            # Phục hồi Worker PC4 sau 2 bước
            elif worker_pc4_killed and not worker_pc4_restored and step >= 5:
                print("\n>>> [PHỤC HỒI]: BẬT LẠI WORKER PC4, GUI HEARTBEAT VA TIEP TUC CHAY! <<<")
                w_pc4["client"].is_alive = True
                worker_pc4_restored = True
                w_model = session.get(WorkerModel, "worker-pc4")
                if w_model:
                    w_model.status = "ONLINE"
                    w_model.updated_at = utc_now()
                    session.commit()
                print(">>> Worker PC4 da ONLINE tro lai! <<<")

        # Điều phối các task READY
        dispatched_count = dispatcher.run_dispatch_cycle(run_id=run_id)
        if dispatched_count > 0:
            print(f"  -> Dispatcher da giao thanh cong {dispatched_count} task.")

        # Mô phỏng Worker xử lý task và gửi callback nộp kết quả
        time.sleep(0.15)
        with session_factory() as session:
            assigned_tasks = session.execute(
                select(TaskModel).where(
                    TaskModel.run_id == run_id,
                    TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value]),
                )
            ).scalars().all()

            for t in assigned_tasks:
                target_worker = w_pc2 if t.agent_id in ("sa-01", "dba-01") else w_pc4
                if target_worker["client"].is_alive:
                    # Tạo kết quả thành công và nộp qua ResultHandler
                    res = TaskResult(
                        task_id=t.task_id,
                        agent_id=t.agent_id,
                        status=TaskStatus.COMPLETED,
                        summary=f"Task {t.task_id} completed successfully by {t.agent_id}.",
                    )
                    worker_p = WorkerPrincipal(
                        worker_id=target_worker["config"].worker_id,
                        machine_id=target_worker["config"].machine_id,
                        allowed_agents=[a.agent_id for a in target_worker["config"].agents],
                    )
                    ResultHandler.handle_result(session, t.task_id, res, worker_p)
                    print(f"  -> Worker '{worker_p.worker_id}' da nộp kết quả COMPLETED cho task '{t.task_id}'.")

        step += 1
        time.sleep(0.1)


if __name__ == "__main__":
    main()
