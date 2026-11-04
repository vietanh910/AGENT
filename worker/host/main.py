"""Điểm khởi chạy chính của Worker Host (worker/host/main.py).

Quản lý toàn bộ vòng đời Worker Host:
- Nạp cấu hình từ worker.yaml.
- Khởi tạo Outbox SQLite và RunnerRegistry.
- Khởi động FastAPI Server.
- Đăng ký và gửi Heartbeat định kỳ về PC1 Control Node.
- Cơ chế Tắt sạch (Graceful Shutdown):
  + Ngừng nhận task mới (trả về 503).
  + Chờ các task đang chạy hoàn thành (hoặc hủy).
  + Quét và gửi sạch toàn bộ kết quả trong Outbox về PC1 trước khi thoát.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import signal
import sys
from typing import AsyncGenerator, Optional

import uvicorn
from fastapi import FastAPI

from agent_contract.enums import AgentRole
from worker.fake_agent import FakeAgent
from worker.host.api import create_worker_api
from worker.host.config import WorkerHostConfig
from worker.host.heartbeat import HeartbeatClient
from worker.host.outbox import OutboxService
from worker.host.runner import RunnerRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("worker_host")


def build_worker_app(
    config: Optional[WorkerHostConfig] = None,
    custom_agents: Optional[dict] = None,
) -> FastAPI:
    """Tạo instance FastAPI hoàn chỉnh của Worker Host."""
    cfg = config or WorkerHostConfig.load_from_yaml()
    outbox = OutboxService(db_path=cfg.outbox_db_path)
    runner_registry = RunnerRegistry(outbox=outbox, secret=cfg.secret, control_url=cfg.control_url)

    # Đăng ký các Agent cho Worker
    if custom_agents:
        for aid, agent_obj in custom_agents.items():
            agent_cfg = next((a for a in cfg.agents if a.agent_id == aid), None)
            role_enum = AgentRole(agent_cfg.role) if agent_cfg else AgentRole.DEV
            runner_registry.register_runner(aid, role_enum, agent_obj)
    else:
        # Tự động nạp hoặc dùng FakeAgent mặc định
        for a in cfg.agents:
            role_enum = AgentRole(a.role)
            # Khởi tạo FakeAgent cho Worker Host
            fake_agent = FakeAgent(agent_id=a.agent_id, role=role_enum)
            runner_registry.register_runner(a.agent_id, role_enum, fake_agent)

    heartbeat_client = HeartbeatClient(config=cfg, runner_registry=runner_registry, outbox=outbox)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        logger.info(f"Worker Host '{cfg.worker_id}' ({cfg.machine_id}) đang khởi động...")
        # 1. Đăng ký với PC1 (thử lại nền để không block server startup)
        reg_ok = await heartbeat_client.register_worker(max_retries=3, initial_delay=0.5)
        if not reg_ok:
            logger.warning(f"Chưa kết nối được với PC1 ngay khi bật. Sẽ tiếp tục thử lại trong heartbeat loop.")

        # 2. Khởi chạy Heartbeat background loop
        hb_task = asyncio.create_task(heartbeat_client.run_heartbeat_loop())

        yield

        # 3. TẮT SẠCH (Graceful Shutdown)
        logger.info(f"Worker Host '{cfg.worker_id}' bắt đầu quy trình tắt sạch...")
        app.state.is_shutting_down = True

        # Ngừng heartbeat loop
        heartbeat_client.stop()
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        # Chờ các task đang chạy hoàn tất (tối đa 5s)
        logger.info("Chờ các task đang chạy hoàn thành...")
        idle_ok = runner_registry.wait_for_all_idle(timeout_seconds=5.0)
        if not idle_ok:
            logger.warning("Một số task chưa kịp hoàn thành, tiến hành ngắt.")
            for tid in runner_registry.get_running_tasks():
                runner_registry.cancel_task(tid)

        # Gửi cạn toàn bộ kết quả còn lại trong Outbox về PC1
        logger.info("Gửi sạch outbox trước khi thoát...")
        await outbox.drain_all_synchronous(timeout_seconds=5.0)
        logger.info(f"Worker Host '{cfg.worker_id}' đã tắt an toàn.")

    app = create_worker_api(cfg, runner_registry, outbox)
    app.router.lifespan_context = lifespan
    app.state.config = cfg
    app.state.outbox = outbox
    app.state.runner_registry = runner_registry
    app.state.heartbeat_client = heartbeat_client

    return app


def main() -> None:
    """Entry point chạy qua dòng lệnh."""
    cfg = WorkerHostConfig.load_from_yaml()
    app = build_worker_app(cfg)
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port)


if __name__ == "__main__":
    main()
