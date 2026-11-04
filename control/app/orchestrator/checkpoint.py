"""checkpoint.py - Lớp bọc lưu trữ điểm kiểm tra (CheckpointStore) cho LangGraph Orchestrator.

Tuân thủ nguyên tắc Prompt S9:
- Trạng thái thật của run và task luôn nằm trong các bảng MySQL.
- Dùng checkpointer là bổ sung, bọc sau interface CheckpointStore để có thể thay thế.
- Khi checkpointer không dùng được (lỗi mạng, không kết nối được bảng checkpoint),
  hệ thống vẫn hoạt động và khôi phục bình thường từ các bảng chính.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from control.app.config import settings

logger = logging.getLogger(__name__)


class CheckpointStore(ABC):
    """Giao diện trừu tượng CheckpointStore bọc checkpointer của LangGraph."""

    @abstractmethod
    def save_checkpoint(
        self,
        run_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Lưu lại trạng thái điểm kiểm tra."""

    @abstractmethod
    def load_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Tải lại trạng thái điểm kiểm tra."""

    @abstractmethod
    def get_checkpointer(self) -> Optional[BaseCheckpointSaver]:
        """Lấy đối tượng BaseCheckpointSaver để truyền vào compile StateGraph."""


class MemoryCheckpointStore(CheckpointStore):
    """Lưu trữ điểm kiểm tra trong bộ nhớ (dùng cho test hoặc chạy độc lập)."""

    def __init__(self) -> None:
        self._saver = MemorySaver()
        self._manual_storage: Dict[str, Dict[str, Any]] = {}

    def save_checkpoint(
        self,
        run_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._manual_storage[run_id] = {
            "state": state,
            "metadata": metadata or {},
        }

    def load_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        entry = self._manual_storage.get(run_id)
        return entry.get("state") if entry else None

    def get_checkpointer(self) -> Optional[BaseCheckpointSaver]:
        return self._saver


class MySQLCheckpointStore(CheckpointStore):
    """Lưu trữ điểm kiểm tra vào MySQL bằng langgraph-checkpoint-mysql (PyMySQLSaver)."""

    def __init__(self, db_url: Optional[str] = None) -> None:
        self.db_url = db_url or settings.DATABASE_URL
        self._saver: Optional[BaseCheckpointSaver] = None
        self._init_saver()

    def _init_saver(self) -> None:
        try:
            from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
            # Chuyển đổi sqlalchemy URL sang chuỗi kết nối chuẩn nếu cần
            conn_url = self.db_url.replace("mysql+pymysql://", "mysql://")
            # PyMySQLSaver yêu cầu autocommit=True
            self._saver = PyMySQLSaver.from_conn_string(conn_url)
            try:
                self._saver.setup()
                logger.info("Đã khởi tạo và kiểm tra bảng checkpointer MySQL thành công.")
            except Exception as e:
                logger.warning(f"Không thể setup bảng checkpointer MySQL ({e}). Sẽ bỏ qua nếu bảng đã tồn tại.")
        except Exception as exc:
            logger.warning(
                f"Không thể khởi tạo MySQLCheckpointStore ({exc}). "
                f"Hệ thống sẽ chạy bình thường dựa trên các bảng MySQL chính thức mà không bị gián đoạn."
            )
            self._saver = None

    def save_checkpoint(
        self,
        run_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self._saver:
            return
        try:
            config = {"configurable": {"thread_id": run_id}}
            checkpoint = {
                "v": 1,
                "ts": "",
                "id": str(state.get("current_stage", "step")),
                "channel_values": dict(state),
                "channel_versions": {},
                "versions_seen": {},
            }
            self._saver.put(config, checkpoint, metadata or {}, {})
        except Exception as exc:
            logger.warning(f"Lỗi khi ghi checkpoint MySQL cho run '{run_id}' ({exc}). Bỏ qua an toàn.")

    def load_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        if not self._saver:
            return None
        try:
            config = {"configurable": {"thread_id": run_id}}
            checkpoint_tuple = self._saver.get_tuple(config)
            if checkpoint_tuple and checkpoint_tuple.checkpoint:
                return checkpoint_tuple.checkpoint.get("channel_values")
        except Exception as exc:
            logger.warning(f"Lỗi khi đọc checkpoint MySQL cho run '{run_id}' ({exc}). Trả về None.")
        return None

    def get_checkpointer(self) -> Optional[BaseCheckpointSaver]:
        return self._saver


class NullCheckpointStore(CheckpointStore):
    """CheckpointStore rỗng: hoàn toàn không dùng checkpointer phụ trợ."""

    def save_checkpoint(self, run_id: str, state: Dict[str, Any], metadata: Optional[Dict[str, Any]] = None) -> None:
        pass

    def load_checkpoint(self, run_id: str) -> Optional[Dict[str, Any]]:
        return None

    def get_checkpointer(self) -> Optional[BaseCheckpointSaver]:
        return None


def get_checkpoint_store(mode: str = "mysql") -> CheckpointStore:
    """Factory helper lấy CheckpointStore theo cấu hình."""
    if mode == "memory":
        return MemoryCheckpointStore()
    elif mode == "null":
        return NullCheckpointStore()
    else:
        try:
            return MySQLCheckpointStore()
        except Exception:
            return MemoryCheckpointStore()
