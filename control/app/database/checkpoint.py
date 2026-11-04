"""Adapter CheckpointStore cho LangGraph.

Bọc thư viện `langgraph-checkpoint-mysql` (hoặc InMemorySaver cho test)
để Orchestrator không gọi trực tiếp thư viện bên ngoài.
MySQL tables (workflow_runs, tasks) vẫn là nguồn sự thật duy nhất;
checkpoint chỉ dùng để snapshot/restore trạng thái đồ thị LangGraph.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
import pymysql

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.mysql.pymysql import PyMySQLSaver
from langgraph.checkpoint.memory import InMemorySaver


class BaseCheckpointStore(ABC):
    """Interface trừu tượng cho Checkpoint Store của Orchestrator."""

    @abstractmethod
    def setup(self) -> None:
        """Khởi tạo bảng/schema cần thiết cho checkpoint."""
        pass

    @abstractmethod
    def put_state(
        self,
        thread_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        checkpoint_ns: str = "",
    ) -> str:
        """Lưu một snapshot trạng thái của luồng thực thi (thread_id = run_id)."""
        pass

    @abstractmethod
    def get_state(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Đọc snapshot trạng thái mới nhất của luồng thực thi."""
        pass

    @abstractmethod
    def get_checkpointer(self) -> Any:
        """Trả về instance checkpointer để truyền vào LangGraph StateGraph.compile()."""
        pass


class MySQLCheckpointStore(BaseCheckpointStore):
    """Cài đặt Checkpoint Store dựa trên langgraph-checkpoint-mysql (PyMySQL)."""

    def __init__(self, conn_string: Optional[str] = None) -> None:
        raw_url = conn_string or os.getenv(
            "DATABASE_URL",
            "mysql+pymysql://ai_user:ai_password_change_me@127.0.0.1:3307/ai_team?charset=utf8mb4",
        )
        # Chuẩn hóa connection string cho PyMySQL
        clean_url = raw_url.replace("mysql+pymysql://", "mysql://")
        if "?charset=" in clean_url:
            clean_url = clean_url.split("?charset=")[0]
        self.conn_string = clean_url
        self._conn: Optional[pymysql.Connection] = None
        self._saver: Optional[PyMySQLSaver] = None

    def _get_connection(self) -> pymysql.Connection:
        if self._conn is None or not self._conn.open:
            params = PyMySQLSaver.parse_conn_string(self.conn_string)
            self._conn = pymysql.connect(
                **params,
                autocommit=True,
                charset="utf8mb4",
            )
        return self._conn

    def get_checkpointer(self) -> PyMySQLSaver:
        if self._saver is None:
            conn = self._get_connection()
            self._saver = PyMySQLSaver(conn)
        return self._saver

    def setup(self) -> None:
        """Tạo bảng checkpoint và đồng bộ collation tương thích với JSON_TABLE trong MySQL 8.4."""
        saver = self.get_checkpointer()
        saver.setup()

        # Đảm bảo các bảng checkpoint dùng utf8mb4_0900_ai_ci để khớp với kết quả JSON_TABLE
        conn = self._get_connection()
        with conn.cursor() as cur:
            for tbl in ["checkpoint_blobs", "checkpoints", "checkpoint_writes"]:
                cur.execute(
                    f"ALTER TABLE {tbl} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )

    def put_state(
        self,
        thread_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        checkpoint_ns: str = "",
    ) -> str:
        saver = self.get_checkpointer()
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }
        cp = empty_checkpoint()
        # Lưu các channel giá trị
        for k, v in state.items():
            cp["channel_values"][k] = v
            cp["channel_versions"][k] = 1

        versions = {k: 1 for k in state.keys()}
        res = saver.put(config, cp, metadata or {}, versions)
        return str(res.get("configurable", {}).get("checkpoint_id", ""))

    def get_state(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> Optional[Dict[str, Any]]:
        saver = self.get_checkpointer()
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }
        loaded = saver.get(config)
        if not loaded:
            return None
        return loaded.get("channel_values", {})

    def close(self) -> None:
        if self._conn is not None and self._conn.open:
            self._conn.close()
            self._conn = None


class MemoryCheckpointStore(BaseCheckpointStore):
    """Cài đặt Checkpoint Store in-memory dùng InMemorySaver cho unit test offline."""

    def __init__(self) -> None:
        self.saver = InMemorySaver()

    def setup(self) -> None:
        pass

    def get_checkpointer(self) -> InMemorySaver:
        return self.saver

    def put_state(
        self,
        thread_id: str,
        state: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
        checkpoint_ns: str = "",
    ) -> str:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }
        cp = empty_checkpoint()
        for k, v in state.items():
            cp["channel_values"][k] = v
            cp["channel_versions"][k] = 1

        versions = {k: 1 for k in state.keys()}
        res = self.saver.put(config, cp, metadata or {}, versions)
        return str(res.get("configurable", {}).get("checkpoint_id", ""))

    def get_state(
        self,
        thread_id: str,
        checkpoint_ns: str = "",
    ) -> Optional[Dict[str, Any]]:
        config = {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": checkpoint_ns,
            }
        }
        loaded = self.saver.get(config)
        if not loaded:
            return None
        return loaded.get("channel_values", {})
