"""Wrapper Redis cho Control Node PC1.

Bao gồm:
- Hàng đợi công việc theo agent (`queue:agent:{agent_id}`)
- Khóa phân tán (`lock:task:{task_id}`) có TTL 5 phút, hỗ trợ tự gia hạn
- Khóa chống gọi trùng lặp (idempotency: `{prefix}:{key}`)
- Hỗ trợ fakeredis để test hoàn toàn offline không cần Redis thật
"""

from __future__ import annotations

import os
import time
import uuid
import threading
from abc import ABC, abstractmethod
from typing import Any, Optional, Iterator
from contextlib import contextmanager

import redis

# Lua script để giải phóng khóa phân tán an toàn (chỉ xóa khi đúng owner token)
RELEASE_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Lua script để gia hạn thời gian sống TTL của khóa phân tán
RENEW_LOCK_LUA = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def get_redis_client(
    redis_url: Optional[str] = None,
    use_fake: bool = False,
    decode_responses: bool = True,
) -> redis.Redis:
    """Khởi tạo Redis client (hoặc FakeRedis nếu use_fake=True hoặc APP_ENV=test)."""
    if use_fake:
        import fakeredis
        return fakeredis.FakeRedis(decode_responses=decode_responses)

    url = redis_url or os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    return redis.Redis.from_url(url, decode_responses=decode_responses)


class BaseQueueBackend(ABC):
    """Interface trừu tượng cho hàng đợi công việc của agent."""

    @abstractmethod
    def push_task(self, agent_id: str, task_id: str) -> int:
        """Đẩy task_id vào đuôi hàng đợi của agent."""
        pass

    @abstractmethod
    def pop_task(self, agent_id: str, timeout_seconds: int = 0) -> Optional[str]:
        """Lấy task_id ra khỏi đầu hàng đợi của agent (blocking nếu timeout > 0)."""
        pass

    @abstractmethod
    def get_queue_length(self, agent_id: str) -> int:
        """Lấy độ dài hàng đợi hiện tại của agent."""
        pass

    @abstractmethod
    def clear_queue(self, agent_id: str) -> None:
        """Xóa hàng đợi của agent (phục vụ dọn dẹp hoặc test)."""
        pass


class RedisQueueBackend(BaseQueueBackend):
    """Cài đặt hàng đợi theo agent sử dụng Redis List (RPUSH/BLPOP)."""

    def __init__(self, client: redis.Redis) -> None:
        self.client = client

    def _key(self, agent_id: str) -> str:
        return f"queue:agent:{agent_id}"

    def push_task(self, agent_id: str, task_id: str) -> int:
        return self.client.rpush(self._key(agent_id), task_id)

    def pop_task(self, agent_id: str, timeout_seconds: int = 0) -> Optional[str]:
        if timeout_seconds > 0:
            result = self.client.blpop([self._key(agent_id)], timeout=timeout_seconds)
            if result:
                # result là tuple (key_name, value)
                return str(result[1])
            return None
        item = self.client.lpop(self._key(agent_id))
        return str(item) if item is not None else None

    def get_queue_length(self, agent_id: str) -> int:
        return int(self.client.llen(self._key(agent_id)))

    def clear_queue(self, agent_id: str) -> None:
        self.client.delete(self._key(agent_id))


class BaseLockBackend(ABC):
    """Interface trừu tượng cho khóa phân tán."""

    @abstractmethod
    def acquire(self, lock_key: str, ttl_seconds: int = 300, token: Optional[str] = None) -> Optional[str]:
        """Chiếm khóa. Trả về token định danh nếu thành công, None nếu thất bại."""
        pass

    @abstractmethod
    def release(self, lock_key: str, token: str) -> bool:
        """Giải phóng khóa an toàn."""
        pass

    @abstractmethod
    def renew(self, lock_key: str, token: str, extension_seconds: int = 300) -> bool:
        """Gia hạn TTL của khóa."""
        pass


class RedisLockBackend(BaseLockBackend):
    """Cài đặt khóa phân tán sử dụng Redis SET NX EX và Lua scripts."""

    def __init__(self, client: redis.Redis) -> None:
        self.client = client
        self._release_script = self.client.register_script(RELEASE_LOCK_LUA)
        self._renew_script = self.client.register_script(RENEW_LOCK_LUA)

    def acquire(self, lock_key: str, ttl_seconds: int = 300, token: Optional[str] = None) -> Optional[str]:
        tok = token or str(uuid.uuid4())
        # SET key value NX EX ttl
        acquired = self.client.set(lock_key, tok, nx=True, ex=ttl_seconds)
        return tok if acquired else None

    def release(self, lock_key: str, token: str) -> bool:
        try:
            result = self._release_script(keys=[lock_key], args=[token])
            return bool(result)
        except redis.exceptions.ResponseError:
            # Dự phòng cho FakeRedis khi không có thư viện Lua (lupa)
            val = self.client.get(lock_key)
            if val == token:
                return bool(self.client.delete(lock_key))
            return False

    def renew(self, lock_key: str, token: str, extension_seconds: int = 300) -> bool:
        try:
            result = self._renew_script(keys=[lock_key], args=[token, extension_seconds])
            return bool(result)
        except redis.exceptions.ResponseError:
            # Dự phòng cho FakeRedis khi không có thư viện Lua (lupa)
            val = self.client.get(lock_key)
            if val == token:
                return bool(self.client.expire(lock_key, extension_seconds))
            return False


class DistributedLock:
    """Quản lý khóa phân tán dạng Context Manager có thread tự gia hạn TTL.
    
    Mặc định TTL = 300 giây (5 phút) theo yêu cầu kiến trúc.
    """

    def __init__(
        self,
        lock_backend: BaseLockBackend,
        lock_key: str,
        ttl_seconds: int = 300,
        auto_renew: bool = False,
        renew_interval_seconds: Optional[int] = None,
    ) -> None:
        self.lock_backend = lock_backend
        self.lock_key = lock_key
        self.ttl_seconds = ttl_seconds
        self.auto_renew = auto_renew
        self.renew_interval = renew_interval_seconds or max(1, ttl_seconds // 3)
        self.token: Optional[str] = None
        self._stop_renew_event: Optional[threading.Event] = None
        self._renew_thread: Optional[threading.Thread] = None

    def acquire(self) -> bool:
        self.token = self.lock_backend.acquire(self.lock_key, ttl_seconds=self.ttl_seconds)
        if self.token is None:
            return False

        if self.auto_renew:
            self._start_auto_renew()
        return True

    def release(self) -> bool:
        if self.auto_renew:
            self._stop_auto_renew()

        if self.token is not None:
            ok = self.lock_backend.release(self.lock_key, self.token)
            self.token = None
            return ok
        return False

    def renew(self, extension_seconds: Optional[int] = None) -> bool:
        if self.token is None:
            return False
        sec = extension_seconds or self.ttl_seconds
        return self.lock_backend.renew(self.lock_key, self.token, extension_seconds=sec)

    def _start_auto_renew(self) -> None:
        self._stop_renew_event = threading.Event()

        def _worker() -> None:
            while not self._stop_renew_event.wait(timeout=self.renew_interval):
                if not self.renew():
                    break

        self._renew_thread = threading.Thread(target=_worker, daemon=True, name=f"lock-renew-{self.lock_key}")
        self._renew_thread.start()

    def _stop_auto_renew(self) -> None:
        if self._stop_renew_event is not None:
            self._stop_renew_event.set()
        if self._renew_thread is not None and self._renew_thread.is_alive():
            self._renew_thread.join(timeout=1.0)

    def __enter__(self) -> DistributedLock:
        if not self.acquire():
            raise RuntimeError(f"Không thể chiếm khóa phân tán: {self.lock_key}")
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def task_lock(
    task_id: str,
    client: redis.Redis,
    ttl_seconds: int = 300,
    auto_renew: bool = True,
) -> DistributedLock:
    """Tạo DistributedLock cho task_id (`lock:task:{task_id}`) chuẩn TTL 5 phút."""
    backend = RedisLockBackend(client)
    return DistributedLock(
        lock_backend=backend,
        lock_key=f"lock:task:{task_id}",
        ttl_seconds=ttl_seconds,
        auto_renew=auto_renew,
    )


class IdempotencyManager:
    """Quản lý khóa chống gọi trùng lặp (idempotency) cho webhook Linear / GitHub."""

    def __init__(self, client: redis.Redis, default_ttl_seconds: int = 3600) -> None:
        self.client = client
        self.default_ttl = default_ttl_seconds

    def check_and_claim(self, idempotency_key: str, ttl_seconds: Optional[int] = None) -> bool:
        """Kiểm tra và chiếm khóa idempotency.
        
        Trả về True nếu đây là lần đầu tiên khóa được xử lý (thành công).
        Trả về False nếu khóa này đã được tiếp nhận trước đó trong khoảng thời gian TTL.
        """
        ttl = ttl_seconds or self.default_ttl
        key = f"idempotency:{idempotency_key}"
        # Trả về True nếu key mới được tạo, False nếu đã tồn tại
        return bool(self.client.set(key, str(int(time.time())), nx=True, ex=ttl))

    def is_claimed(self, idempotency_key: str) -> bool:
        """Kiểm tra xem key đã được nhận trước đó chưa."""
        return bool(self.client.exists(f"idempotency:{idempotency_key}"))

    def release(self, idempotency_key: str) -> None:
        """Giải phóng key (nếu cần xử lý lại khi có lỗi bất thường)."""
        self.client.delete(f"idempotency:{idempotency_key}")
