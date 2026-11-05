"""Bộ đệm kết quả ngoại tuyến Outbox cho Worker Host (worker/host/outbox.py).

Nguyên tắc bắt buộc:
1. Kết quả TaskResult PHẢI được ghi bền vững vào SQLite cục bộ (worker/data/outbox.db) TRƯỚC khi gửi.
2. Gửi POST callback_url (hoặc /api/v1/tasks/{id}/result) kèm Bearer secret.
3. CHỈ XÓA khỏi SQLite khi PC1 phản hồi HTTP 200.
4. Nếu chưa gửi được (mất mạng, PC1 chưa bật), lưu trạng thái để gửi lại với backoff và tiếp tục sau khi khởi động lại Worker.
5. Bộ nhớ đệm kết quả theo task_id hỗ trợ xử lý idempotency nhanh chóng.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from agent_contract.models import TaskResult

logger = logging.getLogger(__name__)


class OutboxService:
    """Quản lý hàng đợi Outbox bền vững trên SQLite cho Worker Host."""

    def __init__(
        self,
        db_path: str = "worker/data/outbox.db",
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._custom_client = http_client
        self._memory_cache: Dict[str, TaskResult] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._drain_task: Optional[asyncio.Task] = None

        self._init_sqlite()
        self._load_cache()

    def _get_connection(self) -> sqlite3.Connection:
        """Tạo kết nối SQLite có timeout và row_factory."""
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite(self) -> None:
        """Khởi tạo bảng outbox_messages nếu chưa có."""
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outbox_messages (
                    task_id TEXT PRIMARY KEY,
                    callback_url TEXT NOT NULL,
                    secret TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at REAL NOT NULL,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def _load_cache(self) -> None:
        """Nạp các kết quả từ SQLite vào memory cache khi khởi động."""
        with self._lock, self._get_connection() as conn:
            rows = conn.execute("SELECT task_id, payload FROM outbox_messages").fetchall()
            for r in rows:
                try:
                    data = json.loads(r["payload"])
                    self._memory_cache[r["task_id"]] = TaskResult(**data)
                except Exception as e:
                    logger.warning(f"Không thể deserialize cached TaskResult cho {r['task_id']}: {e}")

    def save_result(
        self,
        task_id: str,
        callback_url: str,
        secret: str,
        result: TaskResult,
    ) -> None:
        """Ghi kết quả vào SQLite TRƯỚC khi gửi và nạp vào bộ nhớ đệm."""
        payload_str = result.model_dump_json()
        now_ts = time.time()
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            # 1. Lưu vào Memory cache
            self._memory_cache[task_id] = result

            # 2. Ghi bền vững vào SQLite
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO outbox_messages (
                        task_id, callback_url, secret, payload, status, attempts, next_retry_at, created_at
                    ) VALUES (?, ?, ?, ?, 'PENDING', 0, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        callback_url = excluded.callback_url,
                        secret = excluded.secret,
                        payload = excluded.payload,
                        status = 'PENDING', attempts = 0, last_error = NULL,
                        next_retry_at = excluded.next_retry_at
                    """,
                    (task_id, callback_url, secret, payload_str, now_ts, now_iso),
                )
                conn.commit()

        logger.info(f"Đã lưu kết quả task '{task_id}' vào outbox SQLite.")

    def get_cached_result(self, task_id: str) -> Optional[TaskResult]:
        """Lấy kết quả từ cache (idempotency)."""
        with self._lock:
            if task_id in self._memory_cache:
                return self._memory_cache[task_id]

            with self._get_connection() as conn:
                row = conn.execute(
                    "SELECT payload FROM outbox_messages WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if row:
                    try:
                        res = TaskResult(**json.loads(row["payload"]))
                        self._memory_cache[task_id] = res
                        return res
                    except Exception:
                        return None
        return None

    async def flush_task(self, task_id: str, client: Optional[httpx.AsyncClient] = None) -> bool:
        """Thử gửi kết quả của một task cụ thể qua callback_url.

        CHỈ XÓA khỏi SQLite khi phản hồi HTTP 200.
        """
        row = None
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT callback_url, secret, payload, attempts FROM outbox_messages WHERE task_id = ?",
                (task_id,),
            ).fetchone()

        if not row:
            return True  # Đã gửi và xóa trước đó

        callback_url = row["callback_url"]
        secret = row["secret"]
        payload_dict = json.loads(row["payload"])
        attempts = row["attempts"] + 1

        headers = {
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        }

        success = False
        last_error = ""

        try:
            if client:
                resp = await client.post(callback_url, json=payload_dict, headers=headers, timeout=10.0)
            elif self._custom_client:
                resp = await self._custom_client.post(callback_url, json=payload_dict, headers=headers, timeout=10.0)
            else:
                async with httpx.AsyncClient(timeout=10.0) as cli:
                    resp = await cli.post(callback_url, json=payload_dict, headers=headers)

            if resp.status_code == 200:
                success = True
                try:
                    logger.info(f"PC1 đã nhận kết quả task '{task_id}' thành công (200 OK). Xóa khỏi Outbox SQLite.")
                except Exception:
                    pass
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text}"
                try:
                    logger.warning(f"Gửi callback task '{task_id}' thất bại ({last_error}).")
                except Exception:
                    pass

        except Exception as e:
            last_error = str(e)
            try:
                logger.warning(f"Lỗi kết nối khi gửi callback task '{task_id}' tới {callback_url}: {e}")
            except Exception:
                pass

        with self._lock, self._get_connection() as conn:
            if success:
                # XÓA KHỎI OUTBOX
                conn.execute("DELETE FROM outbox_messages WHERE task_id = ? AND payload = ?", (task_id, row["payload"]))
                conn.commit()
                return True
            else:
                # Cập nhật số lần thử và thời điểm thử lại (exponential backoff capped at 30s)
                backoff_delay = min(2 ** (attempts - 1), 30.0)
                next_retry = time.time() + backoff_delay
                conn.execute(
                    """
                    UPDATE outbox_messages
                    SET attempts = ?, next_retry_at = ?, last_error = ?
                    WHERE task_id = ? AND payload = ?
                    """,
                    (attempts, next_retry, last_error, task_id, row["payload"]),
                )
                conn.commit()
                return False

    async def drain_pending(self, client: Optional[httpx.AsyncClient] = None) -> int:
        """Quét và gửi lại tất cả các bản tin đang chờ tới hạn retry trong SQLite."""
        now_ts = time.time()
        pending_task_ids: List[str] = []

        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT task_id FROM outbox_messages WHERE next_retry_at <= ?",
                (now_ts,),
            ).fetchall()
            pending_task_ids = [r["task_id"] for r in rows]

        sent_count = 0
        for tid in pending_task_ids:
            if await self.flush_task(tid, client=client):
                sent_count += 1

        return sent_count

    def get_pending_count(self) -> int:
        """Đếm số bản tin còn tồn đọng trong Outbox."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT count(*) as cnt FROM outbox_messages").fetchone()
            return row["cnt"] if row else 0

    async def drain_all_synchronous(self, timeout_seconds: float = 5.0) -> None:
        """Gửi cạn tất cả outbox trước khi tắt (graceful shutdown)."""
        start = time.time()
        while self.get_pending_count() > 0 and (time.time() - start) < timeout_seconds:
            await self.drain_pending()
            await asyncio.sleep(0.2)
