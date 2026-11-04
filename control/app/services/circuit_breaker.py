"""control/app/services/circuit_breaker.py - Bộ ngắt mạch an toàn (Circuit Breaker) cho Linear, GitHub và Worker Hosts (Prompt S11).

Đặc tả:
- 3 trạng thái chuẩn: CLOSED (bình thường), OPEN (ngắt mạch khi quá ngưỡng lỗi), HALF_OPEN (thử nghiệm phục hồi sau recovery_timeout).
- Circuit breaker riêng biệt cho:
  + Linear API (tránh spam khi Linear sập hoặc trả 429)
  + GitHub API (tránh nghẽn khi GitHub rớt mạng hoặc 5xx)
  + Từng Worker Host cụ thể (tránh spam worker bị offline/treo)
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple, TypeVar

from agent_contract.enums import ErrorCode

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitState(str, enum.Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreakerOpenError(Exception):
    """Ngoại lệ phát sinh khi Circuit Breaker đang ở trạng thái OPEN."""

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = retry_after
        super().__init__(
            f"Mạch '{name}' đang ngắt (OPEN). Dịch vụ tạm thời không khả dụng, thử lại sau {retry_after:.1f}s."
        )


class CircuitBreaker:
    """Bộ ngắt mạch an toàn đơn lẻ (Thread-safe)."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_success_threshold: int = 1,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_success_threshold = half_open_success_threshold

        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._last_failure_time: Optional[float] = None

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._evaluate_state()
            return self._state

    @property
    def consecutive_failures(self) -> int:
        with self._lock:
            return self._consecutive_failures

    def _evaluate_state(self) -> None:
        """Cập nhật trạng thái từ OPEN sang HALF_OPEN nếu đã hết recovery_timeout."""
        if self._state == CircuitState.OPEN and self._last_failure_time is not None:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                logger.info(f"CircuitBreaker '{self.name}': recovery_timeout đã hết ({elapsed:.1f}s >= {self.recovery_timeout}s). Chuyển OPEN -> HALF_OPEN.")
                self._state = CircuitState.HALF_OPEN
                self._consecutive_successes = 0

    def can_execute(self) -> bool:
        """Kiểm tra xem yêu cầu có được phép thực thi hay không."""
        with self._lock:
            self._evaluate_state()
            if self._state == CircuitState.CLOSED:
                return True
            if self._state == CircuitState.HALF_OPEN:
                return True
            return False

    def record_success(self) -> None:
        """Ghi nhận một lời gọi thành công."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._consecutive_successes += 1
                if self._consecutive_successes >= self.half_open_success_threshold:
                    logger.info(f"CircuitBreaker '{self.name}': Đạt ngưỡng phục hồi thành công. Chuyển HALF_OPEN -> CLOSED.")
                    self._state = CircuitState.CLOSED
                    self._consecutive_failures = 0
                    self._last_failure_time = None
            elif self._state == CircuitState.CLOSED:
                self._consecutive_failures = 0

    def record_failure(self) -> None:
        """Ghi nhận một lời gọi thất bại."""
        with self._lock:
            self._consecutive_failures += 1
            self._last_failure_time = time.time()

            if self._state == CircuitState.HALF_OPEN:
                logger.warning(f"CircuitBreaker '{self.name}': Thử nghiệm thất bại ở HALF_OPEN. Chuyển về OPEN.")
                self._state = CircuitState.OPEN
            elif self._state == CircuitState.CLOSED:
                if self._consecutive_failures >= self.failure_threshold:
                    logger.warning(
                        f"CircuitBreaker '{self.name}': Vượt ngưỡng lỗi ({self._consecutive_failures} >= {self.failure_threshold}). Chuyển CLOSED -> OPEN."
                    )
                    self._state = CircuitState.OPEN

    def reset(self) -> None:
        """Đặt lại trạng thái về ban đầu (dùng cho test hoặc can thiệp thủ công)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._consecutive_failures = 0
            self._consecutive_successes = 0
            self._last_failure_time = None

    def execute(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Bọc một hàm gọi với kiểm soát Circuit Breaker."""
        if not self.can_execute():
            retry_after = max(0.0, self.recovery_timeout - (time.time() - (self._last_failure_time or 0.0)))
            raise CircuitBreakerOpenError(self.name, retry_after=retry_after)

        try:
            res = func(*args, **kwargs)
            self.record_success()
            return res
        except Exception:
            self.record_failure()
            raise


class CircuitBreakerRegistry:
    """Quản lý các instance CircuitBreaker tập trung."""

    _instances: Dict[str, CircuitBreaker] = {}
    _lock = threading.Lock()

    @classmethod
    def get(
        cls,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> CircuitBreaker:
        with cls._lock:
            if name not in cls._instances:
                cls._instances[name] = CircuitBreaker(
                    name=name,
                    failure_threshold=failure_threshold,
                    recovery_timeout=recovery_timeout,
                )
            return cls._instances[name]

    @classmethod
    def get_linear(cls) -> CircuitBreaker:
        return cls.get("linear", failure_threshold=4, recovery_timeout=20.0)

    @classmethod
    def get_github(cls) -> CircuitBreaker:
        return cls.get("github", failure_threshold=4, recovery_timeout=20.0)

    @classmethod
    def get_worker(cls, worker_id: str) -> CircuitBreaker:
        return cls.get(f"worker:{worker_id}", failure_threshold=3, recovery_timeout=15.0)

    @classmethod
    def reset_all(cls) -> None:
        with cls._lock:
            for cb in cls._instances.values():
                cb.reset()
            cls._instances.clear()
