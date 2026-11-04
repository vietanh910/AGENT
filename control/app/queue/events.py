"""Phát và đọc sự kiện hệ thống cho Control Node PC1.

Kết hợp Redis Stream (`events:run:{run_id}`) để UI/Log aggregator lắng nghe theo thời gian thực
và ghi nhận bền vững vào bảng `agent_events` trong MySQL.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Union

import redis
from sqlalchemy.orm import Session

from agent_contract.system_models import SystemEventType
from control.app.database.models import AgentEventModel


class EventPublisher:
    """Bộ phát sự kiện hệ thống đa kênh (Redis Stream + MySQL)."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    def _stream_key(self, run_id: Optional[str]) -> str:
        return f"events:run:{run_id}" if run_id else "events:system"

    def publish(
        self,
        event_type: Union[SystemEventType, str],
        payload: Dict[str, Any],
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        worker_id: Optional[str] = None,
        session: Optional[Session] = None,
    ) -> str:
        """Phát sự kiện lên Redis Stream và lưu vào MySQL nếu có db session.
        
        Trả về Redis message ID.
        """
        ev_type_str = event_type.value if isinstance(event_type, SystemEventType) else str(event_type)
        now_utc = datetime.now(timezone.utc)
        event_id = str(uuid.uuid4())

        # Dữ liệu đẩy lên Redis Stream (mọi giá trị là string/json)
        message_data = {
            "event_id": event_id,
            "event_type": ev_type_str,
            "run_id": run_id or "",
            "task_id": task_id or "",
            "agent_id": agent_id or "",
            "worker_id": worker_id or "",
            "payload": json.dumps(payload, ensure_ascii=False),
            "timestamp": now_utc.isoformat(),
        }

        # Đẩy vào stream của run cụ thể và stream tổng hệ thống
        target_stream = self._stream_key(run_id)
        msg_id = f"fallback-{event_id}"
        if self.redis is not None:
            try:
                raw_id = self.redis.xadd(target_stream, message_data, maxlen=5000, approximate=True)
                msg_id = str(raw_id)
                if run_id:
                    # Đồng thời broadcast lên events:system để log aggregator theo dõi tập trung
                    self.redis.xadd("events:system", message_data, maxlen=10000, approximate=True)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(f"Redis publish gặp lỗi (chế độ suy giảm): {e}")

        # Lưu bền vững vào MySQL nếu có session
        if session is not None:
            db_event = AgentEventModel(
                event_id=event_id,
                event_type=ev_type_str,
                worker_id=worker_id,
                task_id=task_id,
                run_id=run_id,
                payload=payload,
                created_at=now_utc,
            )
            session.add(db_event)
            session.flush()

        return str(msg_id)


class EventSubscriber:
    """Bộ lắng nghe sự kiện từ Redis Stream."""

    def __init__(self, redis_client: redis.Redis) -> None:
        self.redis = redis_client

    def _stream_key(self, run_id: Optional[str]) -> str:
        return f"events:run:{run_id}" if run_id else "events:system"

    def read_events(
        self,
        run_id: Optional[str] = None,
        last_id: str = "0-0",
        count: int = 100,
    ) -> List[Dict[str, Any]]:
        """Đọc danh sách sự kiện từ stream tính từ last_id."""
        stream_key = self._stream_key(run_id)
        raw_events = self.redis.xrange(stream_key, min=last_id, max="+", count=count)

        result: List[Dict[str, Any]] = []
        for msg_id, fields in raw_events:
            if msg_id == last_id:
                continue
            entry = self._parse_fields(msg_id, fields)
            result.append(entry)
        return result

    def stream_events(
        self,
        run_id: Optional[str] = None,
        last_id: str = "$",
        block_ms: int = 2000,
        max_events: Optional[int] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Generator stream sự kiện liên tục (phục vụ Server-Sent Events / WebSocket)."""
        stream_key = self._stream_key(run_id)
        current_id = last_id
        count = 0

        while True:
            # XREAD block
            items = self.redis.xread({stream_key: current_id}, block=block_ms, count=50)
            if items:
                for _, messages in items:
                    for msg_id, fields in messages:
                        entry = self._parse_fields(msg_id, fields)
                        current_id = msg_id
                        yield entry
                        count += 1
                        if max_events and count >= max_events:
                            return
            else:
                # Không có event mới trong block_ms
                pass

    def _parse_fields(self, msg_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
        """Giải mã fields từ Redis Stream."""
        res: Dict[str, Any] = {"stream_msg_id": msg_id}
        for k, v in fields.items():
            key_str = k.decode("utf-8") if isinstance(k, bytes) else str(k)
            val_str = v.decode("utf-8") if isinstance(v, bytes) else str(v)
            if key_str == "payload":
                try:
                    res[key_str] = json.loads(val_str)
                except Exception:
                    res[key_str] = val_str
            else:
                res[key_str] = val_str
        return res


def get_event_publisher(redis_client: Optional[redis.Redis] = None) -> EventPublisher:
    """Hàm tiện ích lấy EventPublisher singleton / mặc định."""
    if redis_client is None:
        try:
            from control.app.queue.redis import get_redis_client
            redis_client = get_redis_client()
        except Exception:
            redis_client = None
    return EventPublisher(redis_client)

