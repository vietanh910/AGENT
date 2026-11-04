"""Script chứng minh hoạt động của FastAPI Gateway & Agent Registry (Prompt S4).

Thực hiện:
1. Gọi GET /health không cần token (trả về 200 OK kèm tình trạng DB, Redis).
2. Nạp mặc định 10 agent và kiểm tra GET /api/v1/agents qua Admin Token.
3. Đăng ký 1 worker (worker-pc2) với secret đúng, xác minh agent chuyển sang ONLINE.
4. Gửi heartbeat định kỳ từ worker-pc2.
5. Giả lập ngắt heartbeat quá 45s (cập nhật last_heartbeat_at trong DB lùi 50s),
   kích hoạt watchdog kiểm tra stale worker -> worker và agent tự chuyển sang OFFLINE.
6. Worker-pc2 gửi heartbeat trở lại -> tự động khôi phục ONLINE.
7. Thử nghiệm bảo mật: token sai trả 401 AgentError, worker can thiệp tài nguyên trái phép trả 403 AgentError.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Đảm bảo root repo nằm trong sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx
from sqlalchemy import select

from agent_contract import CAPABILITY_REGISTRY, AgentRole, AgentStatus
from agent_contract.system_models import Heartbeat, HeartbeatAgentStatus, WorkerRegistration
from control.app.api.dependencies import get_db, get_redis
from control.app.config import settings
from control.app.database.models import AgentModel, WorkerModel
from control.app.database.session import get_session_factory
from control.app.main import app
from control.app.queue.redis import get_redis_client
from control.app.services.heartbeat import check_and_update_stale_workers
from control.app.services.registry import RegistryService

ADMIN_TOKEN = settings.ADMIN_TOKEN
PC2_SECRET = "worker_pc2_secret_token_change_me"


async def main() -> None:
    print("=" * 70)
    print("DEMO S4: FASTAPI GATEWAY & AGENT REGISTRY TRÊN PC1")
    print("=" * 70)

    # Khởi tạo DB session factory và Redis client
    session_factory = get_session_factory()
    redis_client = get_redis_client()

    # 0. Khởi tạo 10 Agent mặc định trong DB
    with session_factory() as db:
        RegistryService(db).init_defaults()
        print("[0] Da khoi tao 10 agent mac dinh vao bang agents.")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8000") as client:
        # 1. GET /health không cần token
        print("\n--- 1. GOI GET /health (KHONG CAN TOKEN) ---")
        resp = await client.get("/health")
        print(f"Status Code: {resp.status_code}")
        print(f"Response: {json.dumps(resp.json(), indent=2)}")
        assert resp.status_code == 200

        # 2. GET /api/v1/agents với Admin Token
        print("\n--- 2. GET /api/v1/agents (ADMIN TOKEN) ---")
        resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        print(f"Status Code: {resp.status_code}")
        agents = resp.json()
        print(f"Tong so agent: {len(agents)}")
        for a in agents:
            print(f"  - {a['agent_id']:<12} | Role: {a['role']:<10} | Machine: {a['machine']:<5} | Status: {a['status']}")

        # 3. Đăng ký worker-pc2 với secret đúng
        print("\n--- 3. DANG KY WORKER (POST /api/v1/workers/register) ---")
        pc2_registration = {
            "worker_id": "worker-pc2",
            "machine_id": "PC2",
            "base_url": "http://192.168.1.102:8000",
            "contract_version": "1.2.0",
            "worker_version": "1.0.0",
            "agents": [
                {
                    "contract_version": "1.2.0",
                    "agent_id": "sa-01",
                    "role": AgentRole.SA.value,
                    "name": "Software Architect",
                    "version": "1.0.0",
                    "capabilities": list(CAPABILITY_REGISTRY[AgentRole.SA]),
                    "status": "IDLE",
                },
                {
                    "contract_version": "1.2.0",
                    "agent_id": "dba-01",
                    "role": AgentRole.DBA.value,
                    "name": "Database Administrator",
                    "version": "1.0.0",
                    "capabilities": list(CAPABILITY_REGISTRY[AgentRole.DBA]),
                    "status": "IDLE",
                },
            ],
        }
        resp = await client.post(
            "/api/v1/workers/register",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json=pc2_registration,
        )
        print(f"Status Code: {resp.status_code}")
        print(f"Response: {resp.json()}")
        assert resp.status_code == 200

        # Kiểm tra lại trạng thái agent của worker-pc2 sau đăng ký
        resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        pc2_agents = [a for a in resp.json() if a["machine"] == "PC2"]
        print("Trang thai cac agent thuoc PC2 sau dang ky:")
        for a in pc2_agents:
            print(f"  - {a['agent_id']}: Status={a['status']}")
            assert a["status"] == "IDLE"

        # 4. Gửi Heartbeat từ worker-pc2
        print("\n--- 4. GUI HEARTBEAT (POST /api/v1/workers/heartbeat) ---")
        hb_payload = {
            "worker_id": "worker-pc2",
            "running_tasks": ["TASK-001"],
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "agents": [
                {"agent_id": "sa-01", "status": AgentStatus.RUNNING.value, "current_task": "TASK-001"},
                {"agent_id": "dba-01", "status": AgentStatus.IDLE.value, "current_task": None},
            ],
        }
        resp = await client.post(
            "/api/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json=hb_payload,
        )
        print(f"Status Code: {resp.status_code}")
        print(f"Response: {resp.json()}")
        assert resp.status_code == 200

        # Kiểm tra sa-01 chuyển sang running, task TASK-001
        resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        sa01 = next(a for a in resp.json() if a["agent_id"] == "sa-01")
        print(f"sa-01 sau Heartbeat: Status={sa01['status']}, current_task={sa01['current_task']}")
        assert sa01["status"] == "RUNNING"
        assert sa01["current_task"] == "TASK-001"

        # 5. Giả lập quá 45s không gửi Heartbeat
        print("\n--- 5. GIA LAP WORKER BI NGAT HEARTBEAT QUA 45S ---")
        with session_factory() as db:
            worker = db.scalar(select(WorkerModel).where(WorkerModel.worker_id == "worker-pc2"))
            if worker:
                worker.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=50)
                db.commit()
            print("Da dieu chinh last_heartbeat_at cua worker-pc2 lui ve 50 giay truoc.")

            # Chạy watchdog kiểm tra stale worker
            events = check_and_update_stale_workers(
                session=db,
                timeout_seconds=45,
            )
            print(f"Watchdog da phat hien {len(events)} su kien chuyen doi trang thai.")

        # Kiểm tra lại agent PC2
        resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        pc2_agents_after_timeout = [a for a in resp.json() if a["machine"] == "PC2"]
        print("Trang thai cac agent PC2 sau khi watchdog xu ly:")
        for a in pc2_agents_after_timeout:
            print(f"  - {a['agent_id']}: Status={a['status']}")
            assert a["status"] == "OFFLINE"

        # 6. Worker-pc2 gửi Heartbeat trở lại
        print("\n--- 6. WORKER-PC2 GUI HEARTBEAT TRO LAI ---")
        hb_recovery = {
            "worker_id": "worker-pc2",
            "running_tasks": [],
            "sent_at": datetime.now(timezone.utc).isoformat(),
            "agents": [
                {"agent_id": "sa-01", "status": AgentStatus.IDLE.value, "current_task": None},
                {"agent_id": "dba-01", "status": AgentStatus.IDLE.value, "current_task": None},
            ],
        }
        resp = await client.post(
            "/api/v1/workers/heartbeat",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json=hb_recovery,
        )
        print(f"Status Code: {resp.status_code}")
        assert resp.status_code == 200

        resp = await client.get("/api/v1/agents", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        pc2_recovered = [a for a in resp.json() if a["machine"] == "PC2"]
        print("Trang thai cac agent PC2 sau khi phuc hoi:")
        for a in pc2_recovered:
            print(f"  - {a['agent_id']}: Status={a['status']}")
            assert a["status"] == "IDLE"

        # 7. Kiểm tra ma trận lỗi (401 và 403)
        print("\n--- 7. KIEM TRA XAC THUC & PHAN QUYEN THEO AgentError SCHEMA ---")
        # 401 khi token sai
        resp_401 = await client.get("/api/v1/agents", headers={"Authorization": "Bearer SAI_TOKEN"})
        print(f"Goi sai token: HTTP {resp_401.status_code} -> Body: {resp_401.json()}")
        assert resp_401.status_code == 401
        assert resp_401.json()["error"]["code"] == "PERMISSION_DENIED"

        # 403 khi worker gọi endpoint cua Admin
        resp_403 = await client.post(
            "/api/v1/runs",
            headers={"Authorization": f"Bearer {PC2_SECRET}"},
            json={"project_id": "PRJ-X", "brief": "Forbidden call"},
        )
        print(f"Worker goi POST /runs: HTTP {resp_403.status_code} -> Body: {resp_403.json()}")
        assert resp_403.status_code == 403
        assert resp_403.json()["error"]["code"] == "PERMISSION_DENIED"

        print("\n" + "=" * 70)
        print("TAT CA CAC MUC KIEM CHUNG CUA S4 DA HOAN THANH VA CHINH XAC 100%!")
        print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
