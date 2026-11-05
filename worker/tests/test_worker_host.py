"""Kiểm thử toàn diện Worker Host, Outbox, Runner và FakeAgent (S5).

Bao gồm:
- API Worker Host: /health, /agents, /tasks (202, 409, 422).
- Tính Idempotent khi giao trùng task_id.
- Tính bền vững của Outbox SQLite (chỉ xóa khi PC1 trả 200, gửi lại khi lỗi).
- Hủy phối hợp (cooperative cancellation).
- Xử lý timeout của task trên Worker.
- Các hành vi của FakeAgent.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from agent_contract.enums import AgentRole, ErrorCode, TaskStatus
from agent_contract.models import TaskRequest, TaskResult
from agent_contract.system_models import DispatchRequest
from worker.fake_agent import BehaviorConfig, FakeAgent, FakeBehavior
from worker.host.api import create_worker_api
from worker.host.config import WorkerAgentConfig, WorkerHostConfig
from worker.host.outbox import OutboxService
from worker.host.runner import RunnerRegistry


@pytest.fixture
def temp_outbox_db(tmp_path: Path) -> str:
    """Đường dẫn SQLite database tạm thời cho test."""
    db_file = tmp_path / "test_outbox.db"
    return str(db_file)


@pytest.fixture
def worker_config(temp_outbox_db: str) -> WorkerHostConfig:
    """Cấu hình Worker Host mẫu cho PC2."""
    return WorkerHostConfig(
        worker_id="test-worker-pc2",
        machine_id="PC2",
        control_url="http://127.0.0.1:8000",
        secret="test_secret_pc2",
        listen_host="127.0.0.1",
        listen_port=8000,
        outbox_db_path=temp_outbox_db,
        agents=[
            WorkerAgentConfig(
                agent_id="sa-01",
                role="SA",
                name="Software Architect",
                capabilities=["system_architecture", "tech_stack_selection"],
            ),
            WorkerAgentConfig(
                agent_id="dba-01",
                role="DBA",
                name="Database Administrator",
                capabilities=["schema_design", "query_optimization"],
            ),
        ],
    )


@pytest.fixture
def outbox_service(temp_outbox_db: str) -> OutboxService:
    return OutboxService(db_path=temp_outbox_db)


@pytest.fixture
def runner_registry(outbox_service: OutboxService, worker_config: WorkerHostConfig) -> RunnerRegistry:
    registry = RunnerRegistry(outbox=outbox_service, secret=worker_config.secret)
    # Gắn FakeAgent cho sa-01 và dba-01
    fake_sa = FakeAgent(agent_id="sa-01", role=AgentRole.SA)
    fake_dba = FakeAgent(agent_id="dba-01", role=AgentRole.DBA)
    registry.register_runner("sa-01", AgentRole.SA, fake_sa)
    registry.register_runner("dba-01", AgentRole.DBA, fake_dba)
    return registry


@pytest.fixture
async def worker_client(
    worker_config: WorkerHostConfig,
    runner_registry: RunnerRegistry,
    outbox_service: OutboxService,
):
    app = create_worker_api(worker_config, runner_registry, outbox_service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testworker",
        headers={"Authorization": f"Bearer {worker_config.secret}"},
    ) as client:
        yield client


@pytest.mark.asyncio
class TestWorkerHostAPI:
    """1. Kiểm tra các endpoint API trên Worker Host."""

    async def test_recovery_generation_does_not_reuse_old_cached_result(self, worker_client, runner_registry, outbox_service, monkeypatch):
        old = TaskResult(task_id="task-new-generation", agent_id="sa-01", status="COMPLETED", summary="Old result",
            metadata={"dispatch_token": "old"})
        outbox_service.save_result(old.task_id, "http://control/callback", "secret", old)
        submitted = []
        monkeypatch.setattr(runner_registry, "submit_task", lambda request: submitted.append(request) or True)
        payload = {"task_id": old.task_id, "project_id": "PRJ-1", "agent_id": "sa-01", "role": "SA",
            "action": "design_architecture", "instruction": "Recreate the architecture", "callback_url": "http://control/callback",
            "context": {"dispatch_token": "new"}}
        response = await worker_client.post("/tasks", json=payload)
        assert response.status_code == 202 and len(submitted) == 1
        assert response.json().get("reason") != "ALREADY_COMPLETED"
        payload["context"]["dispatch_token"] = "old"
        response = await worker_client.post("/tasks", json=payload)
        assert response.json()["reason"] == "ALREADY_COMPLETED" and len(submitted) == 1

    async def test_recovery_does_not_treat_an_old_running_generation_as_the_new_one(self, worker_client, runner_registry):
        runner = runner_registry.get_runner("sa-01")
        runner.current_task_id, runner.current_dispatch_token = "task-still-running", "old"
        payload = {"task_id": "task-still-running", "project_id": "PRJ-1", "agent_id": "sa-01", "role": "SA",
            "action": "design_architecture", "instruction": "Recover architecture", "callback_url": "http://control/callback",
            "context": {"dispatch_token": "new"}}
        assert (await worker_client.post("/tasks", json=payload)).status_code == 409
        runner.current_task_id, runner.current_dispatch_token = None, None

    async def test_unauthenticated_request_returns_401(self, worker_client: httpx.AsyncClient):
        # Gọi không có token (xóa header Authorization) phải nhận 401
        resp = await worker_client.get("/agents", headers={"Authorization": ""})
        assert resp.status_code == 401
        assert resp.json()["detail"]["code"] == ErrorCode.PERMISSION_DENIED.value

    async def test_invalid_token_returns_401(self, worker_client: httpx.AsyncClient):
        # Gọi với token sai phải nhận 401
        resp = await worker_client.get("/agents", headers={"Authorization": "Bearer wrong_secret"})
        assert resp.status_code == 401

    async def test_health_check(self, worker_client: httpx.AsyncClient):
        resp = await worker_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert data["worker_id"] == "test-worker-pc2"
        assert data["machine_id"] == "PC2"

    async def test_list_and_get_agents(self, worker_client: httpx.AsyncClient):
        resp = await worker_client.get("/agents")
        assert resp.status_code == 200
        agents = resp.json()
        assert len(agents) == 2
        assert {a["agent_id"] for a in agents} == {"sa-01", "dba-01"}

        resp_sa = await worker_client.get("/agents/sa-01")
        assert resp_sa.status_code == 200
        assert resp_sa.json()["role"] == "SA"

        resp_404 = await worker_client.get("/agents/dev-01")
        assert resp_404.status_code == 404

    async def test_dispatch_invalid_role_or_action_returns_422(self, worker_client: httpx.AsyncClient):
        # 1. Gửi task tới agent không thuộc worker
        req_unknown_agent = {
            "task_id": "task-unknown",
            "project_id": "PRJ-1",
            "agent_id": "dev-01",  # Không thuộc PC2
            "role": "DEV",
            "action": "implement_feature",
            "instruction": "Code feature",
            "callback_url": "http://control/callback",
        }
        resp = await worker_client.post("/tasks", json=req_unknown_agent)
        assert resp.status_code == 422
        assert resp.json()["accepted"] is False

        # 2. Gửi action sai role (sa-01 nhận action của DEV)
        req_wrong_action = {
            "task_id": "task-wrong",
            "project_id": "PRJ-1",
            "agent_id": "sa-01",
            "role": "SA",
            "action": "implement_feature",  # Sai action cho SA
            "instruction": "Design system",
            "callback_url": "http://control/callback",
        }
        resp2 = await worker_client.post("/tasks", json=req_wrong_action)
        assert resp2.status_code == 422
        assert resp2.json()["accepted"] is False

    async def test_dispatch_success_returns_202(
        self,
        worker_client: httpx.AsyncClient,
        runner_registry: RunnerRegistry,
    ):
        req = {
            "task_id": "task-sa-001",
            "project_id": "PRJ-1",
            "agent_id": "sa-01",
            "role": "SA",
            "action": "design_architecture",
            "instruction": "Design the system architecture",
            "callback_url": "http://control/callback",
            "timeout_seconds": 60,
        }
        resp = await worker_client.post("/tasks", json=req)
        assert resp.status_code == 202
        data = resp.json()
        assert data["accepted"] is True
        assert data["task_id"] == "task-sa-001"

    async def test_dispatch_busy_agent_returns_409(
        self,
        worker_client: httpx.AsyncClient,
        runner_registry: RunnerRegistry,
    ):
        # Cấu hình sa-01 HANG để giữ agent luôn bận
        sa_runner = runner_registry.get_runner("sa-01")
        sa_runner.agent_instance.configure_action("design_architecture", BehaviorConfig(behavior=FakeBehavior.HANG))

        # Task 1 được nhận
        req1 = {
            "task_id": "task-sa-hang",
            "project_id": "PRJ-1",
            "agent_id": "sa-01",
            "role": "SA",
            "action": "design_architecture",
            "instruction": "Hang task",
            "callback_url": "http://control/callback",
        }
        resp1 = await worker_client.post("/tasks", json=req1)
        assert resp1.status_code == 202

        # Cho thread khởi chạy một chút
        await asyncio.sleep(0.05)

        # Task 2 gửi tới sa-01 khi đang bận -> 409 Conflict
        req2 = {
            "task_id": "task-sa-conflict",
            "project_id": "PRJ-1",
            "agent_id": "sa-01",
            "role": "SA",
            "action": "design_architecture",
            "instruction": "Conflict task",
            "callback_url": "http://control/callback",
        }
        resp2 = await worker_client.post("/tasks", json=req2)
        assert resp2.status_code == 409
        assert resp2.json()["accepted"] is False
        assert resp2.json()["reason"] == "BUSY"

        # Giải phóng task hang bằng cancel
        await worker_client.post("/tasks/task-sa-hang/cancel")
        await asyncio.sleep(0.1)
        sa_runner.agent_instance.reset()

    async def test_dispatch_idempotency_duplicate_task_id(
        self,
        worker_client: httpx.AsyncClient,
        runner_registry: RunnerRegistry,
        outbox_service: OutboxService,
    ):
        # 1. Giao task hợp lệ
        req = {
            "task_id": "task-idemp-01",
            "project_id": "PRJ-1",
            "agent_id": "dba-01",
            "role": "DBA",
            "action": "design_schema",
            "instruction": "Design MySQL schema",
            "callback_url": "http://control/callback",
        }
        resp1 = await worker_client.post("/tasks", json=req)
        assert resp1.status_code == 202

        # 2. Giao lại ngay task_id đó khi đang chạy -> vẫn trả 202 (ALREADY_RUNNING) không lỗi 409
        resp2 = await worker_client.post("/tasks", json=req)
        assert resp2.status_code == 202
        assert resp2.json()["accepted"] is True

        # Chờ task chạy xong
        await asyncio.sleep(0.15)

        # 3. Giao lại task_id đó sau khi đã hoàn thành -> vẫn trả 202 (ALREADY_COMPLETED)
        resp3 = await worker_client.post("/tasks", json=req)
        assert resp3.status_code == 202
        assert resp3.json()["accepted"] is True


@pytest.mark.asyncio
class TestOutboxResilience:
    """2. Kiểm tra độ bền vững của Outbox SQLite."""

    async def test_old_flush_cannot_delete_a_new_recovery_result(self, temp_outbox_db):
        outbox = OutboxService(db_path=temp_outbox_db)
        old = TaskResult(task_id="task-recovery-race", agent_id="sa-01", status="COMPLETED", summary="Old generation",
            metadata={"dispatch_token": "old"})
        new = old.model_copy(update={"summary": "New generation", "metadata": {"dispatch_token": "new"}})
        outbox.save_result(old.task_id, "http://control/callback", "secret", old)

        async def response_while_new_result_arrives(request):
            outbox.save_result(new.task_id, "http://control/callback", "secret", new)
            return httpx.Response(200, json={"status": "ignored_stale_attempt"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response_while_new_result_arrives)) as client:
            assert await outbox.flush_task(old.task_id, client)
        assert outbox.get_pending_count() == 1
        assert outbox.get_cached_result(new.task_id).metadata["dispatch_token"] == "new"

    async def test_save_before_send_and_delete_only_on_200(self, temp_outbox_db: str):
        outbox = OutboxService(db_path=temp_outbox_db)

        # Tạo một TaskResult hoàn tất
        res = TaskResult(
            task_id="task-outbox-01",
            agent_id="sa-01",
            status=TaskStatus.COMPLETED,
            summary="Completed architecture design",
        )

        outbox.save_result(
            task_id="task-outbox-01",
            callback_url="http://mock-control/api/v1/tasks/task-outbox-01/result",
            secret="test_secret",
            result=res,
        )

        # Kết quả đã có trong SQLite
        assert outbox.get_pending_count() == 1
        assert outbox.get_cached_result("task-outbox-01") is not None

        # Giả lập PC1 trả 500 Internal Error
        async def mock_handler_500(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "Database error"})

        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler_500))
        success = await outbox.flush_task("task-outbox-01", client=mock_client)
        assert success is False
        # VẪN CÒN trong SQLite
        assert outbox.get_pending_count() == 1

        # Giả lập PC1 trả 200 OK
        async def mock_handler_200(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "accepted"})

        mock_client_200 = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler_200))
        success_200 = await outbox.flush_task("task-outbox-01", client=mock_client_200)
        assert success_200 is True
        # ĐÃ BỊ XÓA khỏi SQLite sau khi 200 OK
        assert outbox.get_pending_count() == 0

    async def test_outbox_persists_across_restart(self, temp_outbox_db: str):
        # 1. Worker phiên bản 1 lưu kết quả nhưng chưa gửi được
        outbox1 = OutboxService(db_path=temp_outbox_db)
        res = TaskResult(
            task_id="task-reboot-01",
            agent_id="dba-01",
            status=TaskStatus.COMPLETED,
            summary="Persisted across crash",
        )
        outbox1.save_result("task-reboot-01", "http://control/cb", "sec", res)
        assert outbox1.get_pending_count() == 1
        del outbox1

        # 2. Worker khởi động lại (phiên bản 2 nạp cùng db_path)
        outbox2 = OutboxService(db_path=temp_outbox_db)
        assert outbox2.get_pending_count() == 1
        cached = outbox2.get_cached_result("task-reboot-01")
        assert cached is not None
        assert cached.summary == "Persisted across crash"


@pytest.mark.asyncio
class TestCooperativeCancellationAndTimeout:
    """3. Kiểm tra hủy phối hợp và timeout trên Worker."""

    async def test_task_cancellation(
        self,
        worker_client: httpx.AsyncClient,
        runner_registry: RunnerRegistry,
        outbox_service: OutboxService,
    ):
        sa_runner = runner_registry.get_runner("sa-01")
        sa_runner.agent_instance.configure_action("design_architecture", BehaviorConfig(behavior=FakeBehavior.HANG))

        # Dispatch task hang
        resp = await worker_client.post("/tasks", json={
            "task_id": "task-to-cancel",
            "project_id": "PRJ-1",
            "agent_id": "sa-01",
            "role": "SA",
            "action": "design_architecture",
            "instruction": "Hang until cancel",
            "callback_url": "http://control/cb",
        })
        assert resp.status_code == 202

        await asyncio.sleep(0.05)

        # Gửi lệnh cancel
        resp_cancel = await worker_client.post("/tasks/task-to-cancel/cancel")
        assert resp_cancel.status_code == 200
        assert resp_cancel.json()["cancelled"] is True

        # Chờ thread kết thúc
        for _ in range(20):
            if not sa_runner.is_busy():
                break
            await asyncio.sleep(0.05)

        # Agent trở lại IDLE
        assert sa_runner.is_busy() is False

        # Outbox ghi nhận kết quả CANCELLED/FAILED
        res = outbox_service.get_cached_result("task-to-cancel")
        assert res is not None
        assert res.status == TaskStatus.FAILED
        assert "cancelled" in res.summary.lower()
