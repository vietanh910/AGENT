"""Dịch vụ đăng ký và Heartbeat định kỳ của Worker Host (worker/host/heartbeat.py).

Tuân thủ:
- Register khi khởi động (thử lại với backoff nếu PC1 chưa lên).
- Gửi Heartbeat định kỳ 10 giây (kèm trạng thái từng agent và task đang chạy).
- Tích hợp gọi quét gửi outbox định kỳ để đảm bảo kết quả bền vững được gửi về PC1.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from typing import Optional

import httpx

from agent_contract.enums import AgentRole, AgentStatus
from agent_contract.models import AgentIdentity
from agent_contract.registry import CAPABILITY_REGISTRY
from agent_contract.system_models import Heartbeat, WorkerRegistration
from worker.host.config import WorkerHostConfig
from worker.host.outbox import OutboxService
from worker.host.runner import RunnerRegistry

logger = logging.getLogger(__name__)


class HeartbeatClient:
    """Client quản lý việc đăng ký và gửi nhịp tim về PC1 Control Node."""

    def __init__(
        self,
        config: WorkerHostConfig,
        runner_registry: RunnerRegistry,
        outbox: OutboxService,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.config = config
        self.runner_registry = runner_registry
        self.outbox = outbox
        self._custom_client = http_client
        self._stop_event = asyncio.Event()

    def _get_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.secret}",
            "Content-Type": "application/json",
        }

    async def register_worker(self, max_retries: int = 10, initial_delay: float = 1.0) -> bool:
        """Đăng ký Worker với PC1 Control Node khi khởi động (thử lại nếu PC1 chưa sẵn sàng)."""
        url = f"{self.config.control_url.rstrip('/')}/api/v1/workers/register"
        headers = self._get_headers()

        # Chuẩn bị danh sách AgentIdentity
        agent_identities: list[AgentIdentity] = []
        for a in self.config.agents:
            role_enum = AgentRole(a.role)
            caps = a.capabilities if a.capabilities else list(CAPABILITY_REGISTRY.get(role_enum, []))
            agent_identities.append(
                AgentIdentity(
                    contract_version=self.config.contract_version,
                    agent_id=a.agent_id,
                    role=role_enum,
                    name=a.name or a.agent_id,
                    version=a.version,
                    capabilities=caps,
                    status=AgentStatus.IDLE,
                )
            )

        payload = WorkerRegistration(
            worker_id=self.config.worker_id,
            machine_id=self.config.machine_id,
            base_url=f"http://{self.config.listen_host}:{self.config.listen_port}",
            agents=agent_identities,
            contract_version=self.config.contract_version,
            worker_version=self.config.worker_version,
        ).model_dump(mode="json")

        delay = initial_delay
        for attempt in range(1, max_retries + 1):
            try:
                logger.info(f"Đang đăng ký Worker '{self.config.worker_id}' tới PC1 ({url}). Lần thử {attempt}/{max_retries}...")
                if self._custom_client:
                    resp = await self._custom_client.post(url, json=payload, headers=headers, timeout=10.0)
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.post(url, json=payload, headers=headers)

                if resp.status_code == 200:
                    logger.info(f"Đăng ký Worker '{self.config.worker_id}' thành công với PC1 Control Node!")
                    return True
                else:
                    logger.warning(f"PC1 từ chối đăng ký với HTTP {resp.status_code}: {resp.text}")

            except Exception as e:
                logger.warning(f"Lỗi kết nối tới PC1 Control Node ({url}): {e}")

            if attempt < max_retries:
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10.0)

        logger.error(f"Đăng ký Worker '{self.config.worker_id}' thất bại sau {max_retries} lần thử.")
        return False

    async def send_heartbeat_once(self) -> bool:
        """Gửi một nhịp tim (Heartbeat) tới PC1."""
        url = f"{self.config.control_url.rstrip('/')}/api/v1/workers/heartbeat"
        headers = self._get_headers()

        hb = Heartbeat(
            worker_id=self.config.worker_id,
            agents=self.runner_registry.get_agent_statuses(),
            running_tasks=self.runner_registry.get_running_tasks(),
            sent_at=datetime.now(timezone.utc),
        )

        try:
            if self._custom_client:
                resp = await self._custom_client.post(url, json=hb.model_dump(mode="json"), headers=headers, timeout=5.0)
            else:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(url, json=hb.model_dump(mode="json"), headers=headers)

            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Lỗi gửi heartbeat tới PC1: {e}")
            return False

    async def run_heartbeat_loop(self) -> None:
        """Vòng lặp gửi heartbeat định kỳ (10s) và quét outbox."""
        interval = self.config.heartbeat_interval_seconds
        while not self._stop_event.is_set():
            # 1. Gửi Heartbeat
            await self.send_heartbeat_once()

            # 2. Quét Outbox gửi các bản tin chưa xác nhận
            try:
                await self.outbox.drain_pending()
            except Exception as e:
                logger.debug(f"Lỗi khi drain outbox trong heartbeat loop: {e}")

            # Chờ interval hoặc bị ngắt
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        """Ngắt vòng lặp heartbeat."""
        self._stop_event.set()
