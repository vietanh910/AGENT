"""Cấu hình Worker Host (worker/host/config.py).

Đọc cấu hình từ file `worker.yaml` (hoặc biến môi trường), quản lý danh sách agent
chạy trên Worker này.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

from agent_contract.models import CONTRACT_VERSION


class WorkerAgentConfig(BaseModel):
    """Cấu hình của một Agent được host trên Worker."""
    agent_id: str
    role: str
    name: Optional[str] = None
    version: str = "1.0.0"
    path: Optional[str] = None
    capabilities: List[str] = Field(default_factory=list)


class WorkerHostConfig(BaseModel):
    """Cấu hình đầy đủ của một Worker Host."""
    worker_id: str = "worker-default"
    machine_id: str = "PC2"
    control_url: str = "http://127.0.0.1:8000"
    secret: str = "worker_secret_token_change_me"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8000
    contract_version: str = CONTRACT_VERSION
    worker_version: str = "1.0.0"
    heartbeat_interval_seconds: int = 10
    outbox_db_path: str = "worker/data/outbox.db"
    agents: List[WorkerAgentConfig] = Field(default_factory=list)

    @classmethod
    def load_from_yaml(cls, yaml_path: Optional[str | Path] = None) -> WorkerHostConfig:
        """Nạp cấu hình từ file YAML hoặc fallback về giá trị mặc định."""
        target_path = Path(yaml_path) if yaml_path else Path("worker.yaml")
        if not target_path.is_file():
            # Thử tìm worker/worker.yaml hoặc control/worker.yaml
            for alt in [Path("worker/worker.yaml"), Path("worker.example.yaml"), Path("worker/worker.example.yaml")]:
                if alt.is_file():
                    target_path = alt
                    break

        if target_path.is_file():
            with open(target_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                # Cho phép ghi đè một số biến từ môi trường
                if os.getenv("WORKER_ID"):
                    data["worker_id"] = os.getenv("WORKER_ID")
                if os.getenv("CONTROL_URL"):
                    data["control_url"] = os.getenv("CONTROL_URL")
                if os.getenv("WORKER_SECRET"):
                    data["secret"] = os.getenv("WORKER_SECRET")
                if os.getenv("LISTEN_PORT"):
                    data["listen_port"] = int(os.getenv("LISTEN_PORT"))
                return cls(**data)

        # Fallback về cấu hình mặc định
        return cls()
