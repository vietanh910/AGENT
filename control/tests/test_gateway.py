"""Kiểm thử FastAPI Gateway, Agent Registry, Phân quyền và Heartbeat (S4).

Tuân thủ:
- Dùng httpx AsyncClient
- Cơ sở dữ liệu MySQL 8.4 thật
- FakeRedis độc lập
- Kiểm tra toàn bộ 7 yêu cầu trong Prompt S4
"""

from __future__ import annotations

import hashlib
import hmac
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator

import fakeredis
import httpx
import pytest
from sqlalchemy import select

from agent_contract.enums import AgentRole, AgentStatus, ErrorCode, TaskStatus
from agent_contract.models import TaskResult
from agent_contract.system_models import Heartbeat, HeartbeatAgentStatus, SystemEventType, WorkerRegistration
from control.app.api.dependencies import get_db, get_redis
from control.app.config import settings
from control.app.database.models import AgentModel, AuditLogModel, TaskModel, WorkerModel, WorkflowRunModel
from control.app.database.repositories import AuditRepository, TaskRepository, WorkflowRunRepository
from control.app.database.session import get_db_session, get_session_factory
from control.app.main import app
from control.app.queue.events import EventPublisher, EventSubscriber
from control.app.security.authentication import reset_worker_secret_manager
from control.app.services.heartbeat import check_and_update_stale_workers
from control.app.services.registry import RegistryService

ADMIN_TOKEN = settings.ADMIN_TOKEN
PC2_SECRET = "worker_pc2_secret_token_change_me"
PC4_SECRET = "worker_pc4_secret_token_change_me"


@pytest.fixture(scope="module")
def shared_fake_redis():
    """Client FakeRedis dùng chung cho các test trong module."""
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
async def async_client(shared_fake_redis) -> AsyncGenerator[httpx.AsyncClient, None]:
    """AsyncClient kết nối tới app FastAPI kèm override Redis và DB."""
    # Nạp lại cấu hình workers
    reset_worker_secret_manager()

    app.dependency_overrides[get_redis] = lambda: shared_fake_redis

    # Dọn dẹp các run còn treo từ các test trước để không vượt MAX_CONCURRENT_RUNS
    cleanup_sess = get_session_factory()()
    try:
        cleanup_sess.query(WorkflowRunModel).filter(
            WorkflowRunModel.status.in_(["PLANNING", "RUNNING", "AWAITING_APPROVAL"])
        ).update({"status": "COMPLETED"})
        cleanup_sess.commit()
    finally:
        cleanup_sess.close()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.mark.asyncio
class TestAuthenticationAndPermissions:
    """1. Kiểm tra xác thực Token và Ma trận phân quyền."""

    async def test_unauthenticated_request_returns_401(self, async_client: httpx.AsyncClient) -> None:
        # Không có token
        resp = await async_client.get("/api/v1/agents")
        assert resp.status_code == 401
        data = resp.json()
        assert "error" in data
        assert data["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

        # Token sai
        resp_wrong = await async_client.get(
            "/api/v1/agents",
            headers={"Authorization": "Bearer invalid_secret_token_123"},
        )
        assert resp_wrong.status_code == 401

    async def test_admin_only_endpoints_reject_worker_token(self, async_client: httpx.AsyncClient) -> None:
        # Worker PC2 gọi endpoint tạo Run của Admin -> 403 Forbidden
        resp = await async_client.post(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json={"project_id": "PRJ-1", "brief": "Build a portal"},
        )
        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == ErrorCode.PERMISSION_DENIED.value


@pytest.mark.asyncio
class TestAgentRegistry:
    """2. Kiểm tra Agent Registry: GET /agents, đăng ký worker, thẩm định role."""

    async def test_get_agents_returns_10_rows(self, async_client: httpx.AsyncClient) -> None:
        resp = await async_client.get(
            "/api/v1/agents",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert resp.status_code == 200
        agents = resp.json()
        assert len(agents) == 10

        # Kiểm tra cấu trúc từng hàng
        sample = agents[0]
        for field in ["agent_id", "role", "machine", "status", "last_seen", "current_task"]:
            assert field in sample

        # Kiểm tra pm-01 và devops-01 mặc định ONLINE trên PC1
        pm = next(a for a in agents if a["agent_id"] == "pm-01")
        assert pm["status"] == "ONLINE"
        assert pm["machine"] == "PC1"

    async def test_register_worker_with_invalid_role_rejected(self, async_client: httpx.AsyncClient) -> None:
        # Worker PC2 phụ trách SA và DBA, nhưng gửi đăng ký sa-01 với vai trò DEV -> bị từ chối
        reg_payload = {
            "worker_id": "worker-pc2",
            "machine_id": "PC2",
            "base_url": "http://192.168.1.102:8000",
            "contract_version": "1.2.0",
            "worker_version": "1.0.0",
            "agents": [
                {
                    "contract_version": "1.2.0",
                    "agent_id": "sa-01",
                    "role": "DEV",  # SAI ROLE (sa-01 phải là SA)
                    "name": "Software Architect",
                    "version": "1.0.0",
                    "capabilities": ["coding"],
                    "status": "IDLE",
                }
            ],
        }

        resp = await async_client.post(
            "/api/v1/workers/register",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json=reg_payload,
        )
        assert resp.status_code == 400
        err = resp.json()["error"]
        assert "không khớp" in err["message"]

    async def test_register_worker_success(self, async_client: httpx.AsyncClient) -> None:
        # Đăng ký hợp lệ cho worker-pc4 (dev-01, tester-01)
        reg_payload = {
            "worker_id": "worker-pc4",
            "machine_id": "PC4",
            "base_url": "http://192.168.1.104:8000",
            "contract_version": "1.2.0",
            "worker_version": "1.0.0",
            "agents": [
                {
                    "contract_version": "1.2.0",
                    "agent_id": "dev-01",
                    "role": "DEV",
                    "name": "Backend Developer",
                    "version": "1.0.0",
                    "capabilities": ["coding"],
                    "status": "IDLE",
                },
                {
                    "contract_version": "1.2.0",
                    "agent_id": "tester-01",
                    "role": "TESTER",
                    "name": "QA Tester",
                    "version": "1.0.0",
                    "capabilities": ["automated_testing"],
                    "status": "IDLE",
                },
            ],
        }

        resp = await async_client.post(
            "/api/v1/workers/register",
            headers={"Authorization": f"Bearer {PC4_SECRET}"},
            json=reg_payload,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "registered"
        assert data["worker_id"] == "worker-pc4"

        # Tra cứu lại /agents: dev-01 và tester-01 phải chuyển IDLE
        agents_resp = await async_client.get(
            "/api/v1/agents",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        dev_agent = next(a for a in agents_resp.json() if a["agent_id"] == "dev-01")
        assert dev_agent["status"] == "IDLE"
        assert dev_agent["machine"] == "PC4"


@pytest.mark.asyncio
class TestHeartbeatAndLifecycle:
    """3. Kiểm tra Heartbeat và tác vụ nền phát hiện Worker OFFLINE."""

    async def test_heartbeat_updates_agent_status(self, async_client: httpx.AsyncClient) -> None:
        # Gửi Heartbeat từ worker-pc4 báo dev-01 đang bận
        hb_payload = {
            "worker_id": "worker-pc4",
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "running_tasks": ["TASK-HB-01"],
            "agents": [
                {
                    "agent_id": "dev-01",
                    "status": "RUNNING",
                    "current_task": "TASK-HB-01",
                }
            ],
        }

        resp = await async_client.post(
            "/api/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {PC4_SECRET}"},
            json=hb_payload,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "acknowledged"

        # Kiểm tra /agents dev-01 đã sang RUNNING kèm task ID
        agents_resp = await async_client.get(
            "/api/v1/agents",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        dev = next(a for a in agents_resp.json() if a["agent_id"] == "dev-01")
        assert dev["status"] == "RUNNING"
        assert dev["current_task"] == "TASK-HB-01"

    async def test_stale_worker_marked_offline_once_and_recovers(
        self,
        shared_fake_redis,
        async_client: httpx.AsyncClient,
    ) -> None:
        publisher = EventPublisher(shared_fake_redis)

        # 1. Đăng ký worker-pc2 trước
        reg_pc2 = {
            "worker_id": "worker-pc2",
            "machine_id": "PC2",
            "base_url": "http://192.168.1.102:8000",
            "contract_version": "1.2.0",
            "worker_version": "1.0.0",
            "agents": [
                {
                    "contract_version": "1.2.0",
                    "agent_id": "sa-01",
                    "role": "SA",
                    "name": "Architect",
                    "version": "1.0.0",
                    "capabilities": [],
                    "status": "IDLE",
                }
            ],
        }
        await async_client.post(
            "/api/v1/workers/register",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json=reg_pc2,
        )

        with get_db_session() as session:
            # 2. Giả lập lùi last_heartbeat_at về quá khứ 60 giây (vượt ngưỡng 45s)
            w = session.get(WorkerModel, "worker-pc2")
            assert w is not None
            w.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=60)
            session.commit()

            # 3. Kích hoạt watchdog quét timeout
            stale = check_and_update_stale_workers(session, timeout_seconds=45, publisher=publisher)
            assert "worker-pc2" in stale

            # Xác nhận trạng thái đã là OFFLINE
            session.refresh(w)
            assert w.status == "OFFLINE"
            agent_sa = session.get(AgentModel, "sa-01")
            assert agent_sa.status == "OFFLINE"

            # 4. Quét lần 2: Đảm bảo "Chỉ chuyển trạng thái khi thực sự đổi, không phát sự kiện lặp"
            stale_2 = check_and_update_stale_workers(session, timeout_seconds=45, publisher=publisher)
            assert "worker-pc2" not in stale_2

        # 5. Worker kết nối lại và gửi heartbeat -> Chuyển lại ONLINE
        hb_resp = await async_client.post(
            "/api/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json={
                "worker_id": "worker-pc2",
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "running_tasks": [],
                "agents": [{"agent_id": "sa-01", "status": "IDLE", "current_task": None}],
            },
        )
        assert hb_resp.status_code == 200

        with get_db_session() as session:
            w_recovered = session.get(WorkerModel, "worker-pc2")
            assert w_recovered.status == "ONLINE"


@pytest.mark.asyncio
class TestTaskResultSecurityIsolation:
    """4. Kiểm tra Worker chỉ được gửi kết quả của task đã giao cho agent của mình."""

    async def test_worker_a_submitting_result_for_worker_b_task_returns_403(
        self,
        async_client: httpx.AsyncClient,
    ) -> None:
        run_id = f"RUN-SEC-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-DEV-TASK"

        # Tạo 1 task giao cho ba-01 (thuộc worker-pc4)
        with get_db_session() as session:
            WorkflowRunRepository.create_run(
                session,
                {"run_id": run_id, "project_id": "PRJ-SEC", "brief": "Security Task"},
            )
            TaskRepository.create_task(
                session,
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "agent_id": "ba-01",  # Thuộc worker-pc4!
                    "role": "BA",
                    "action": "write_requirements",
                    "instruction": "Write requirements",
                },
            )
            session.commit()

        # Worker PC2 (chỉ quản lý dev-01, scrum_master-01) cố tình nộp kết quả cho task của ba-01!
        malicious_result = {
            "contract_version": "1.2.0",
            "task_id": task_id,
            "agent_id": "ba-01",
            "status": "COMPLETED",
            "summary": "Requirements completed",
            "outputs": [],
        }

        resp = await async_client.post(
            f"/api/v1/tasks/{task_id}/result",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},  # Token của PC2
            json=malicious_result,
        )
        # Phải trả về 403 Forbidden!
        assert resp.status_code == 403
        data = resp.json()
        assert data["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

        # Kiểm tra đã ghi nhận Audit vi phạm bảo mật
        with get_db_session() as session:
            stmt = select(AuditLogModel).where(
                AuditLogModel.action == "TASK_RESULT_FORBIDDEN",
                AuditLogModel.target == task_id,
            )
            audit = session.execute(stmt).scalars().first()
            assert audit is not None
            assert audit.actor == "worker-pc2"
            assert audit.result == "FORBIDDEN_AGENT_MISMATCH"

        # Bây giờ Worker PC4 (chính chủ ba-01) nộp kết quả -> 200 Thành công!
        legit_resp = await async_client.post(
            f"/api/v1/tasks/{task_id}/result",
            headers={"Authorization": f"Bearer {PC4_SECRET}"},  # Token của PC4
            json=malicious_result,
        )
        assert legit_resp.status_code == 200
        assert legit_resp.json()["status"] == "accepted"


@pytest.mark.asyncio
class TestWebhooks:
    """5. Kiểm tra Webhooks Linear và GitHub với chữ ký HMAC-SHA256."""

    async def test_linear_webhook_invalid_signature_returns_401(
        self,
        async_client: httpx.AsyncClient,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", "linear_secret_key_123")
        payload = {"action": "create", "type": "Issue"}

        # Gửi chữ ký sai
        resp = await async_client.post(
            "/api/v1/webhooks/linear",
            headers={"Linear-Signature": "invalid_hex_signature"},
            json=payload,
        )
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    async def test_linear_webhook_valid_signature_accepted(
        self,
        async_client: httpx.AsyncClient,
        monkeypatch,
    ) -> None:
        secret = "linear_secret_key_123"
        monkeypatch.setenv("LINEAR_WEBHOOK_SECRET", secret)
        body_bytes = b'{"action":"update","type":"Issue"}'

        # Tính HMAC-SHA256 chuẩn
        sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()

        resp = await async_client.post(
            "/api/v1/webhooks/linear",
            headers={"Linear-Signature": sig, "Content-Type": "application/json"},
            content=body_bytes,
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "received"

    async def test_github_webhook_signature_verification(
        self,
        async_client: httpx.AsyncClient,
        monkeypatch,
    ) -> None:
        secret = "github_secret_key_456"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", secret)
        body_bytes = b'{"action":"opened","repository":{"full_name":"vietanh910/AGENT"}}'

        # Chữ ký sai
        bad_resp = await async_client.post(
            "/api/v1/webhooks/github",
            headers={"X-Hub-Signature-256": "sha256=invalid"},
            content=body_bytes,
        )
        assert bad_resp.status_code == 401

        # Chữ ký đúng
        sig = hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
        good_resp = await async_client.post(
            "/api/v1/webhooks/github",
            headers={"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"},
            content=body_bytes,
        )
        assert good_resp.status_code == 200
        assert good_resp.json()["status"] == "received"


@pytest.mark.asyncio
class TestWorkflowRunEndpoints:
    """6. Kiểm tra các endpoint điều khiển Workflow Run và Phê duyệt Gates."""

    async def test_run_creation_and_approval_cycle(self, async_client: httpx.AsyncClient) -> None:
        # 1. Admin tạo Run
        run_id = f"RUN-TEST-{uuid.uuid4().hex[:6]}"
        create_resp = await async_client.post(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={
                "run_id": run_id,
                "project_id": "PRJ-FLOW",
                "brief": "Full delivery test",
                "plan": {"stages": ["requirements", "architecture", "coding"]},
            },
        )
        assert create_resp.status_code == 201

        # 2. Xem chi tiết Run và Plan
        get_resp = await async_client.get(
            f"/api/v1/runs/{run_id}",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert get_resp.status_code == 200
        assert get_resp.json()["status"] == "CREATED"

        plan_resp = await async_client.get(
            f"/api/v1/runs/{run_id}/plan",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert plan_resp.status_code == 200
        assert "stages" in plan_resp.json()["plan"]

        # 3. Phê duyệt Gate APPROVE_SCOPE
        approve_resp = await async_client.post(
            f"/api/v1/runs/{run_id}/approve",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            json={"gate": "APPROVE_SCOPE", "notes": "Scope is approved by PO"},
        )
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "approved"

        # 4. Hủy Run
        cancel_resp = await async_client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        )
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"
