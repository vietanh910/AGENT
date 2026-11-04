"""Heartbeat Monitor Service cho Control Node PC1.

Theo dõi sức khỏe các Worker Host:
- Định kỳ quét các Worker quá thời hạn HEARTBEAT_TIMEOUT_SECONDS (45 giây)
- Chuyển trạng thái Worker và toàn bộ Agent phụ thuộc sang OFFLINE
- Phát sự kiện WORKER_OFFLINE lên Redis Stream và bảng agent_events
- Ghi nhận Audit Log
- Đảm bảo "Chỉ chuyển trạng thái khi thực sự đổi, không phát sự kiện lặp"
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from agent_contract.system_models import SystemEventType
from control.app.database.models import AgentModel, WorkerModel
from control.app.database.repositories import AuditRepository
from control.app.database.session import get_db_session
from control.app.queue.events import EventPublisher

logger = logging.getLogger(__name__)


def check_and_update_stale_workers(
    session: Session,
    timeout_seconds: int = 45,
    publisher: Optional[EventPublisher] = None,
) -> List[str]:
    """Kiểm tra và đánh dấu OFFLINE các worker bị mất kết nối quá thời hạn timeout.
    
    Chỉ phát sự kiện và ghi audit khi trạng thái thực sự chuyển từ ONLINE -> OFFLINE.
    """
    now = datetime.now(timezone.utc)
    stmt = select(WorkerModel).where(WorkerModel.status == "ONLINE")
    active_workers = session.execute(stmt).scalars().all()

    stale_worker_ids: List[str] = []

    for worker in active_workers:
        # Không đánh dấu offline worker nội bộ của PC1 nếu đang chạy
        if worker.worker_id == "worker-pc1":
            continue

        is_stale = False
        if worker.last_heartbeat_at is None:
            is_stale = True
        else:
            last_hb = worker.last_heartbeat_at
            if last_hb.tzinfo is None:
                last_hb = last_hb.replace(tzinfo=timezone.utc)
            diff = (now - last_hb).total_seconds()
            if diff > timeout_seconds:
                is_stale = True

        if is_stale:
            # 1. Đổi trạng thái Worker sang OFFLINE
            worker.status = "OFFLINE"
            stale_worker_ids.append(worker.worker_id)
            logger.warning(
                f"Worker '{worker.worker_id}' ({worker.machine_id}) quá hạn heartbeat ({timeout_seconds}s). "
                f"Đánh dấu trạng thái OFFLINE."
            )

            # 2. Đổi trạng thái các agent thuộc worker này sang OFFLINE
            stmt_agents = select(AgentModel).where(AgentModel.machine == worker.machine_id)
            agents = session.execute(stmt_agents).scalars().all()
            for agent in agents:
                agent.status = "OFFLINE"
                agent.updated_at = now

            # 3. Ghi nhận Audit Log
            AuditRepository.record(
                session,
                actor="system:heartbeat_monitor",
                action="WORKER_OFFLINE",
                target=worker.worker_id,
                result="TIMEOUT",
                details={
                    "timeout_seconds": timeout_seconds,
                    "last_heartbeat_at": worker.last_heartbeat_at.isoformat() if worker.last_heartbeat_at else None,
                    "affected_agents": [a.agent_id for a in agents],
                },
            )

            # 4. Phát sự kiện WORKER_OFFLINE (chỉ phát đúng 1 lần khi đổi trạng thái)
            if publisher:
                publisher.publish(
                    event_type=SystemEventType.WORKER_OFFLINE,
                    payload={
                        "worker_id": worker.worker_id,
                        "machine_id": worker.machine_id,
                        "reason": f"Heartbeat timeout > {timeout_seconds}s",
                        "affected_agents": [a.agent_id for a in agents],
                    },
                    worker_id=worker.worker_id,
                    session=session,
                )

    if stale_worker_ids:
        session.commit()

    return stale_worker_ids


async def run_heartbeat_watchdog_loop(
    stop_event: asyncio.Event,
    interval_seconds: float = 5.0,
    timeout_seconds: int = 45,
    publisher: Optional[EventPublisher] = None,
) -> None:
    """Vòng lặp nền quét heartbeat mỗi 5 giây."""
    logger.info(f"Bắt đầu tác vụ nền Heartbeat Watchdog (chu kỳ {interval_seconds}s, timeout {timeout_seconds}s)")
    while not stop_event.is_set():
        try:
            with get_db_session() as session:
                check_and_update_stale_workers(
                    session=session,
                    timeout_seconds=timeout_seconds,
                    publisher=publisher,
                )
        except Exception as e:
            logger.error(f"Lỗi trong vòng lặp Heartbeat Watchdog: {e}")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass
