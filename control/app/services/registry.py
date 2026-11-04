"""Agent Registry Service cho PC1 Control Node.

Quản lý danh bạ 10 AI Agent và tình trạng kết nối của các Worker Host:
- Nạp AGENT_REGISTRY_DEFAULT vào cơ sở dữ liệu khi khởi động
- Xử lý Worker đăng ký (POST /api/v1/workers/register) với thẩm định chặt chẽ agent_id và role
- Xử lý Heartbeat từ Worker (POST /api/v1/workers/heartbeat) cập nhật last_seen, agent status, running tasks
- Cung cấp danh sách tổng quan 10 agent cho endpoint GET /api/v1/agents
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, AgentStatus
from agent_contract.registry import AGENT_REGISTRY_DEFAULT
from agent_contract.system_models import DispatchState, Heartbeat, SystemEventType, WorkerRegistration
from control.app.database.models import AgentModel, AuditLogModel, TaskModel, WorkerModel
from control.app.database.repositories import AuditRepository, WorkerRepository
from control.app.queue.events import EventPublisher

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RegistryError(Exception):
    """Lỗi nghiệp vụ khi thao tác với Agent Registry."""

    def __init__(self, message: str, details: Optional[dict] = None) -> None:
        self.message = message
        self.details = details or {}
        super().__init__(message)


class RegistryService:
    """Service quản lý danh bạ Agent và Worker."""

    def __init__(self, session: Session, publisher: Optional[EventPublisher] = None) -> None:
        self.session = session
        self.publisher = publisher

    def init_defaults(self) -> None:
        """Nạp AGENT_REGISTRY_DEFAULT vào bảng agents nếu chưa có.
        
        Quy ước:
        - Mọi agent ban đầu đặt OFFLINE.
        - Riêng pm-01 và devops-01 do tiến trình PC1 tự quản lý nên báo ONLINE ngay.
        """
        now = utc_now()
        for item in AGENT_REGISTRY_DEFAULT:
            agent_id = item["agent_id"]
            role_val = item["role"].value if isinstance(item["role"], AgentRole) else str(item["role"])
            machine = item["machine"]
            runner = item["runner"]

            existing = self.session.get(AgentModel, agent_id)
            if existing is None:
                # Tạo mới agent
                is_pc1_local = agent_id in ("pm-01", "devops-01")
                status = "ONLINE" if is_pc1_local else "OFFLINE"

                agent = AgentModel(
                    agent_id=agent_id,
                    role=role_val,
                    machine=machine,
                    runner=runner,
                    status=status,
                    current_task=None,
                    capabilities=[],
                    updated_at=now,
                )
                self.session.add(agent)
                logger.info(f"Khởi tạo agent '{agent_id}' ({role_val}) trên {machine} với trạng thái {status}")
            else:
                # Nếu là agent PC1, bảo đảm duy trì ONLINE
                if agent_id in ("pm-01", "devops-01"):
                    existing.status = "ONLINE"
                    existing.updated_at = now
        self.session.flush()

    def register_worker(self, reg: WorkerRegistration) -> WorkerModel:
        """Đăng ký Worker Host và danh sách Agent kèm thẩm định chặt chẽ."""
        now = utc_now()
        registry_map: Dict[str, str] = {
            item["agent_id"]: item["role"].value if isinstance(item["role"], AgentRole) else str(item["role"])
            for item in AGENT_REGISTRY_DEFAULT
        }

        # 1. Thẩm định danh sách agents
        if not reg.agents:
            raise RegistryError(f"Worker '{reg.worker_id}' phải khai báo ít nhất một agent phụ trách.")

        for agent_identity in reg.agents:
            aid = agent_identity.agent_id
            if aid not in registry_map:
                raise RegistryError(
                    f"Agent '{aid}' không nằm trong danh mục AGENT_REGISTRY_DEFAULT của hệ thống.",
                    details={"agent_id": aid},
                )

            expected_role = registry_map[aid].upper()
            provided_role = (
                agent_identity.role.value
                if isinstance(agent_identity.role, AgentRole)
                else str(agent_identity.role)
            ).upper()

            if expected_role != provided_role:
                raise RegistryError(
                    f"Agent '{aid}' có vai trò '{provided_role}' không khớp với cấu hình hệ thống '{expected_role}'.",
                    details={"agent_id": aid, "expected_role": expected_role, "provided_role": provided_role},
                )

        # 2. Cập nhật hoặc tạo Worker
        worker = self.session.get(WorkerModel, reg.worker_id)
        is_reconnecting = False
        if worker is None:
            worker = WorkerModel(
                worker_id=reg.worker_id,
                machine_id=reg.machine_id,
                base_url=str(reg.base_url),
                status="ONLINE",
                last_heartbeat_at=now,
                contract_version=reg.contract_version,
                worker_version=reg.worker_version,
                registered_at=now,
                updated_at=now,
            )
            self.session.add(worker)
            is_reconnecting = True
        else:
            if worker.status != "ONLINE":
                is_reconnecting = True
            worker.machine_id = reg.machine_id
            worker.base_url = str(reg.base_url)
            worker.status = "ONLINE"
            worker.last_heartbeat_at = now
            worker.contract_version = reg.contract_version
            worker.worker_version = reg.worker_version
            worker.updated_at = now

        # 3. Cập nhật các Agent thuộc worker này
        for agent_identity in reg.agents:
            aid = agent_identity.agent_id
            agent = self.session.get(AgentModel, aid)
            caps = [c.value if hasattr(c, "value") else str(c) for c in agent_identity.capabilities]

            if agent is None:
                agent = AgentModel(
                    agent_id=aid,
                    role=registry_map[aid],
                    machine=reg.machine_id,
                    runner="Worker Host",
                    status="IDLE",
                    capabilities=caps,
                    updated_at=now,
                )
                self.session.add(agent)
            else:
                agent.status = "IDLE"
                agent.machine = reg.machine_id
                agent.capabilities = caps
                agent.updated_at = now

        self.session.flush()

        # 4. Phát sự kiện WORKER_ONLINE và ghi audit nếu worker mới hoặc vừa online lại
        if is_reconnecting:
            AuditRepository.record(
                self.session,
                actor=reg.worker_id,
                action="WORKER_ONLINE",
                target=reg.worker_id,
                result="ONLINE",
                details={
                    "machine_id": reg.machine_id,
                    "agents": [a.agent_id for a in reg.agents],
                },
            )
            if self.publisher:
                self.publisher.publish(
                    event_type=SystemEventType.WORKER_ONLINE,
                    payload={
                        "worker_id": reg.worker_id,
                        "machine_id": reg.machine_id,
                        "agents": [a.agent_id for a in reg.agents],
                    },
                    worker_id=reg.worker_id,
                    session=self.session,
                )

        return worker

    def record_heartbeat(self, hb: Heartbeat) -> WorkerModel:
        """Cập nhật heartbeat, trạng thái agent và các task đang chạy."""
        now = utc_now()
        worker = self.session.get(WorkerModel, hb.worker_id)
        if worker is None:
            raise RegistryError(f"Worker '{hb.worker_id}' chưa đăng ký trong hệ thống.")

        was_offline = worker.status == "OFFLINE"
        worker.status = "ONLINE"
        worker.last_heartbeat_at = now

        # Cập nhật trạng thái từng agent của worker
        for agent_status in hb.agents:
            aid = agent_status.agent_id
            agent = self.session.get(AgentModel, aid)
            if agent:
                # agent_status.status có thể là AgentStatus enum hoặc string
                st_val = (
                    agent_status.status.value
                    if isinstance(agent_status.status, AgentStatus)
                    else str(agent_status.status)
                )
                agent.status = st_val
                agent.current_task = agent_status.current_task
                agent.updated_at = now

        # Chuyển trạng thái task từ ASSIGNED sang RUNNING khi worker báo cáo đang chạy
        running_task_ids = set(hb.running_tasks)
        for agent_status in hb.agents:
            if agent_status.current_task:
                running_task_ids.add(agent_status.current_task)

        for tid in running_task_ids:
            task = self.session.get(TaskModel, tid)
            if task and task.dispatch_state == DispatchState.ASSIGNED.value:
                task.dispatch_state = DispatchState.RUNNING.value
                task.updated_at = now
                task.version += 1
                if self.publisher:
                    self.publisher.publish(
                        event_type=SystemEventType.TASK_STARTED,
                        payload={"task_id": tid, "worker_id": hb.worker_id},
                        run_id=task.run_id,
                        task_id=tid,
                        session=self.session,
                    )

        self.session.flush()

        # Nếu trước đó worker bị đánh dấu OFFLINE mà giờ quay lại -> phát sự kiện WORKER_ONLINE
        if was_offline:
            AuditRepository.record(
                self.session,
                actor=hb.worker_id,
                action="WORKER_ONLINE",
                target=hb.worker_id,
                result="ONLINE",
                details={"reason": "Heartbeat restored"},
            )
            if self.publisher:
                self.publisher.publish(
                    event_type=SystemEventType.WORKER_ONLINE,
                    payload={"worker_id": hb.worker_id, "reason": "Heartbeat restored"},
                    worker_id=hb.worker_id,
                    session=self.session,
                )

        return worker

    def get_agents_overview(self) -> List[Dict[str, Any]]:
        """Lấy danh sách 10 agent chuẩn định dạng bảng cho GET /api/v1/agents."""
        stmt = select(AgentModel).order_by(AgentModel.agent_id)
        agents = self.session.execute(stmt).scalars().all()

        result = []
        for a in agents:
            # last_seen tính từ thời điểm cập nhật trạng thái khi agent còn sống
            last_seen_str = a.updated_at.isoformat() if a.status != "OFFLINE" and a.updated_at else None
            result.append({
                "agent_id": a.agent_id,
                "role": a.role,
                "machine": a.machine,
                "status": a.status,
                "last_seen": last_seen_str,
                "current_task": a.current_task,
            })
        return result
