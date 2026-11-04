"""Xác thực Bearer token cho Control Node PC1.

Quản lý và đối soát:
- Token ADMIN (đọc từ biến môi trường ADMIN_TOKEN qua settings)
- Token từng Worker (đọc từ file control/workers.yaml, fallback workers.example.yaml)
- So sánh bảo mật bằng hmac.compare_digest chống timing attack
- Tuyệt đối không log token thô ra output hay database
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml

logger = logging.getLogger(__name__)


def mask_token(token: Optional[str]) -> str:
    """Che giấu token trước khi ghi log hoặc audit."""
    if not token:
        return "[EMPTY]"
    if len(token) <= 6:
        return "***"
    return f"{token[:2]}***{token[-2:]}"


@dataclass(frozen=True)
class AdminPrincipal:
    """Đại diện cho lời gọi từ Quản trị viên (Admin Token)."""
    role: str = "ADMIN"
    actor_id: str = "admin"


@dataclass(frozen=True)
class WorkerPrincipal:
    """Đại diện cho lời gọi từ Worker Host (Worker Token)."""
    worker_id: str
    machine_id: str
    allowed_agents: List[str] = field(default_factory=list)
    role: str = "WORKER"

    @property
    def actor_id(self) -> str:
        return self.worker_id

    def can_operate_agent(self, agent_id: str) -> bool:
        """Kiểm tra worker có quyền đại diện cho agent_id không."""
        return agent_id in self.allowed_agents


Principal = Union[AdminPrincipal, WorkerPrincipal]


class WorkerSecretManager:
    """Quản lý secrets của các worker trong workers.yaml."""

    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config_path = self._resolve_config_path(config_path)
        self.workers: Dict[str, dict] = {}
        self.load()

    def _resolve_config_path(self, path: Optional[str]) -> Path:
        if path:
            p = Path(path)
            if p.exists():
                return p

        # Thử các đường dẫn mặc định
        candidates = [
            Path("control/workers.yaml"),
            Path("workers.yaml"),
            Path("control/workers.example.yaml"),
            Path("workers.example.yaml"),
        ]
        for c in candidates:
            if c.exists():
                return c
        return Path("control/workers.example.yaml")

    def load(self) -> None:
        """Đọc và nạp danh sách worker secrets."""
        if not self.config_path.exists():
            logger.warning(f"Không tìm thấy file cấu hình workers: {self.config_path}")
            self.workers = {}
            return

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                self.workers = data.get("workers", {})
                logger.info(f"Đã nạp {len(self.workers)} worker configs từ {self.config_path}")
        except Exception as e:
            logger.error(f"Lỗi khi đọc file cấu hình workers {self.config_path}: {e}")
            self.workers = {}

    def get_worker_by_secret(self, provided_secret: str) -> Optional[WorkerPrincipal]:
        """Tìm worker có secret trùng khớp bằng hmac.compare_digest."""
        if not provided_secret:
            return None

        for worker_id, info in self.workers.items():
            expected_secret = info.get("secret", "")
            if expected_secret and hmac.compare_digest(provided_secret, expected_secret):
                return WorkerPrincipal(
                    worker_id=worker_id,
                    machine_id=info.get("machine_id", "UNKNOWN"),
                    allowed_agents=list(info.get("allowed_agents", [])),
                )
        return None

    def get_worker(self, worker_id: str) -> Optional[dict]:
        return self.workers.get(worker_id)


# Singleton Secret Manager
_worker_secret_manager: Optional[WorkerSecretManager] = None


def get_worker_secret_manager(path: Optional[str] = None) -> WorkerSecretManager:
    global _worker_secret_manager
    if _worker_secret_manager is None:
        _worker_secret_manager = WorkerSecretManager(path)
    return _worker_secret_manager


def reset_worker_secret_manager(path: Optional[str] = None) -> WorkerSecretManager:
    """Hỗ trợ khởi tạo lại cho test."""
    global _worker_secret_manager
    _worker_secret_manager = WorkerSecretManager(path)
    return _worker_secret_manager


def authenticate_bearer(
    auth_header: Optional[str],
    admin_token: str,
    secret_manager: Optional[WorkerSecretManager] = None,
) -> Optional[Principal]:
    """Xác thực Bearer token từ Authorization header.
    
    Trả về AdminPrincipal nếu là ADMIN_TOKEN,
    Trả về WorkerPrincipal nếu là secret của một worker,
    Trả về None nếu token rỗng hoặc không khớp.
    """
    if not auth_header:
        return None

    parts = auth_header.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None

    token = parts[1].strip()

    # 1. So khớp với ADMIN_TOKEN
    if admin_token and hmac.compare_digest(token, admin_token):
        return AdminPrincipal()

    # 2. So khớp với Worker Secrets
    mgr = secret_manager or get_worker_secret_manager()
    worker_principal = mgr.get_worker_by_secret(token)
    if worker_principal is not None:
        return worker_principal

    return None
