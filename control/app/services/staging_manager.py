"""control/app/services/staging_manager.py - Quản lý triển khai Staging, cấp phát cổng và dọn dẹp Stack (Prompt S10).

Đặc tả:
- Cấp phát cổng từ dải cấu hình (mặc định 8100-8900), tránh đụng cổng giữa các run đồng thời.
- Thư mục triển khai riêng biệt: deploy/staging/<run_id>/
- Tên compose project riêng: staging_<clean_run_id>
- File .env sinh ngoài repo, không chứa bất kỳ secret nào của hệ thống.
- Tuyệt đối không mount docker.sock.
- Giới hạn MAX_STAGING_STACKS và cơ chế dọn dẹp các stack cũ nhất.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from control.app.config import settings
from tools.terminal import ExecutionResult, TerminalExecutor

logger = logging.getLogger(__name__)


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Kiểm tra xem một cổng mạng (port) đã bị chiếm dụng trên host hay chưa."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            target_host = "127.0.0.1" if host in ["0.0.0.0", ""] else host
            if s.connect_ex((target_host, port)) == 0:
                return True
        except Exception:
            pass

    for h in ([host, "127.0.0.1"] if host in ["0.0.0.0", ""] else [host]):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                bind_h = "" if h == "0.0.0.0" else h
                s.bind((bind_h, port))
            except OSError:
                return True
    return False


class PortAllocator:
    """Bộ cấp phát cổng mạng an toàn cho các môi trường Staging cách ly giữa các Run."""

    _instance: Optional[PortAllocator] = None
    _lock = threading.Lock()

    def __init__(
        self,
        port_start: Optional[int] = None,
        port_end: Optional[int] = None,
    ) -> None:
        self.port_start = port_start or getattr(settings, "STAGING_PORT_START", 8100)
        self.port_end = port_end or getattr(settings, "STAGING_PORT_END", 8900)
        self._run_to_port: Dict[str, int] = {}
        self._allocated_ports: Set[int] = set()

    @classmethod
    def get_instance(cls) -> PortAllocator:
        with cls._lock:
            if cls._instance is None:
                cls._instance = PortAllocator()
            return cls._instance

    def allocate_port(self, run_id: str) -> int:
        """Cấp phát một cổng duy nhất cho run_id trong dải port cho phép."""
        with self._lock:
            # Nếu run_id đã được cấp cổng trước đó -> trả về cổng đó
            if run_id in self._run_to_port:
                return self._run_to_port[run_id]

            for port in range(self.port_start, self.port_end + 1):
                if port in self._allocated_ports:
                    continue
                if not is_port_in_use(port):
                    self._allocated_ports.add(port)
                    self._run_to_port[run_id] = port
                    logger.info(f"Đã cấp phát cổng {port} cho Staging Run '{run_id}'.")
                    return port

            raise RuntimeError(
                f"Đã cạn kiệt cổng mạng khả dụng trong dải {self.port_start}-{self.port_end}."
            )

    def get_allocated_port(self, run_id: str) -> Optional[int]:
        """Lấy cổng đã cấp phát cho run_id nếu có."""
        with self._lock:
            return self._run_to_port.get(run_id)

    def release_port(self, run_id: str) -> Optional[int]:
        """Thu hồi cổng đã cấp phát cho run_id."""
        with self._lock:
            port = self._run_to_port.pop(run_id, None)
            if port is not None and port in self._allocated_ports:
                self._allocated_ports.remove(port)
                logger.info(f"Đã thu hồi cổng {port} từ Run '{run_id}'.")
            return port

    def reset(self) -> None:
        """Xóa toàn bộ cổng đã cấp (dùng cho test)."""
        with self._lock:
            self._run_to_port.clear()
            self._allocated_ports.clear()


class StagingManager:
    """Quản trị vòng đời triển khai Staging: chuẩn bị file, dọn dẹp stack cũ, và khởi chạy."""

    def __init__(
        self,
        base_staging_dir: Optional[str | Path] = None,
        max_staging_stacks: Optional[int] = None,
        port_allocator: Optional[PortAllocator] = None,
    ) -> None:
        raw_dir = base_staging_dir or getattr(settings, "STAGING_DEPLOY_DIR", "deploy/staging")
        self.base_dir = Path(raw_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_stacks = max_staging_stacks or getattr(settings, "MAX_STAGING_STACKS", 5)
        self.port_allocator = port_allocator or PortAllocator.get_instance()

    def get_staging_dir(self, run_id: str) -> Path:
        """Trả về đường dẫn tuyệt đối thư mục staging của run_id."""
        clean_id = re.sub(r"[^a-zA-Z0-9_\-]", "_", run_id)
        d = (self.base_dir / clean_id).resolve()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get_compose_project_name(self, run_id: str) -> str:
        """Tên project docker-compose riêng cho run (chữ thường và gạch dưới)."""
        clean = re.sub(r"[^a-zA-Z0-9]", "_", run_id).lower()
        return f"staging_{clean}"

    def cleanup_old_stacks(
        self,
        terminal: Optional[TerminalExecutor] = None,
        execute: bool = False,
    ) -> List[str]:
        """Quét và dọn dẹp các stack Staging cũ vượt quá MAX_STAGING_STACKS (dọn stack cũ nhất trước)."""
        cleaned_runs: List[str] = []
        if not self.base_dir.exists():
            return cleaned_runs

        subdirs = [d for d in self.base_dir.iterdir() if d.is_dir()]
        if len(subdirs) <= self.max_stacks:
            return cleaned_runs

        # Sắp xếp theo thời gian mtime tăng dần (cũ nhất trước)
        subdirs.sort(key=lambda d: d.stat().st_mtime)

        stacks_to_remove = len(subdirs) - self.max_stacks + 1
        for old_dir in subdirs[:stacks_to_remove]:
            run_id = old_dir.name
            proj_name = self.get_compose_project_name(run_id)
            logger.info(f"Dọn dẹp Staging stack cũ: '{run_id}' (project={proj_name})")

            # 1. Gọi docker compose down qua terminal nếu có
            if terminal:
                terminal.add_allowed_directory(old_dir)
                try:
                    terminal.run(
                        cmd_list=["docker", "compose", "-p", proj_name, "down", "-v"],
                        cwd=old_dir,
                        execute=execute,
                    )
                except Exception as e:
                    logger.warning(f"Lỗi khi dừng stack cũ {proj_name}: {e}")

            # 2. Giải phóng cổng
            self.port_allocator.release_port(run_id)

            # 3. Xóa thư mục
            try:
                shutil.rmtree(old_dir, ignore_errors=True)
            except Exception as e:
                logger.warning(f"Không thể xóa thư mục {old_dir}: {e}")

            cleaned_runs.append(run_id)

        return cleaned_runs

    def prepare_staging_stack(
        self,
        run_id: str,
        image_tag: str,
        container_port: int = 8000,
        env_vars: Optional[Dict[str, str]] = None,
        terminal: Optional[TerminalExecutor] = None,
        execute: bool = False,
    ) -> Tuple[Path, int]:
        """Tạo file docker-compose.yml và .env an toàn cho run_id."""
        # 1. Tự động dọn dẹp các stack cũ nếu vượt ngưỡng
        self.cleanup_old_stacks(terminal=terminal, execute=execute)

        # 2. Cấp phát cổng riêng cho run
        port = self.port_allocator.allocate_port(run_id)

        # 3. Tạo thư mục deploy/staging/<run_id>/
        staging_dir = self.get_staging_dir(run_id)
        proj_name = self.get_compose_project_name(run_id)

        # 4. Sinh file .env KHÔNG CHỨA BẤT KỲ SECRET HỆ THỐNG NÀO
        safe_env = {
            "PORT": str(port),
            "APP_ENV": "staging",
            "RUN_ID": run_id,
        }
        if env_vars:
            for k, v in env_vars.items():
                upper_k = k.upper()
                # Chặn các key nhạy cảm của hệ thống
                if any(sec in upper_k for sec in ["SECRET", "TOKEN", "KEY", "PASSWORD"]):
                    continue
                safe_env[k] = v

        env_lines = [f"{k}={v}" for k, v in safe_env.items()]
        env_file = staging_dir / ".env"
        env_file.write_text("\n".join(env_lines) + "\n", encoding="utf-8")

        # 5. Sinh file docker-compose.yml (TUYỆT ĐỐI KHÔNG mount docker.sock)
        compose_content = f"""version: '3.8'

services:
  app:
    image: {image_tag}
    container_name: {proj_name}_app
    ports:
      - "{port}:{container_port}"
    env_file:
      - .env
    restart: unless-stopped
"""
        compose_file = staging_dir / "docker-compose.yml"
        compose_file.write_text(compose_content, encoding="utf-8")

        logger.info(f"Đã chuẩn bị Staging stack tại {staging_dir} (port={port}, image={image_tag})")
        return staging_dir, port

    def deploy_stack(
        self,
        run_id: str,
        terminal: TerminalExecutor,
        execute: bool = False,
    ) -> ExecutionResult:
        """Khởi chạy Staging stack qua docker compose up -d."""
        staging_dir = self.get_staging_dir(run_id)
        proj_name = self.get_compose_project_name(run_id)

        terminal.add_allowed_directory(staging_dir)
        cmd = ["docker", "compose", "-p", proj_name, "up", "-d"]
        return terminal.run(cmd, cwd=staging_dir, execute=execute)

    def teardown_stack(
        self,
        run_id: str,
        terminal: TerminalExecutor,
        execute: bool = False,
    ) -> ExecutionResult:
        """Dừng và thu hồi Staging stack."""
        staging_dir = self.get_staging_dir(run_id)
        proj_name = self.get_compose_project_name(run_id)

        terminal.add_allowed_directory(staging_dir)
        cmd = ["docker", "compose", "-p", proj_name, "down", "-v"]
        res = terminal.run(cmd, cwd=staging_dir, execute=execute)

        self.port_allocator.release_port(run_id)
        shutil.rmtree(staging_dir, ignore_errors=True)
        return res
