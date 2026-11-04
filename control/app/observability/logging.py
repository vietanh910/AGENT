"""control/app/observability/logging.py - Structured JSON Logging với Correlation ID và Secret Masking.

Đặc tả:
- Định dạng JSON có cấu trúc cho mỗi dòng log.
- Tự động gắn các trường tương quan: run_id, task_id, agent_id từ contextvars.
- Bộ lọc che giấu secret (Secret Masking Filter): che password, token, key, Authorization header.
- Tương thích tiêu chuẩn Python logging.
"""

from __future__ import annotations

import contextvars
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Context variables phục vụ truy vết tương quan đa tác vụ
current_run_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_run_id", default=None)
current_task_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_task_id", default=None)
current_agent_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("current_agent_id", default=None)


class LogContext:
    """Context manager thiết lập correlation ID cho luồng thực thi hiện tại."""

    def __init__(
        self,
        run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        self.run_id = run_id
        self.task_id = task_id
        self.agent_id = agent_id
        self._tokens: list[tuple[contextvars.ContextVar, contextvars.Token]] = []

    def __enter__(self) -> "LogContext":
        if self.run_id is not None:
            self._tokens.append((current_run_id, current_run_id.set(self.run_id)))
        if self.task_id is not None:
            self._tokens.append((current_task_id, current_task_id.set(self.task_id)))
        if self.agent_id is not None:
            self._tokens.append((current_agent_id, current_agent_id.set(self.agent_id)))
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        for var, token in reversed(self._tokens):
            var.reset(token)


SECRET_PATTERNS = [
    # Authorization Bearer
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{8,}", re.IGNORECASE), r"\1***REDACTED***"),
    # Token, Key, Secret, Password trong URL hoặc key-value
    (re.compile(r"((?:token|api_key|apikey|secret|password|passwd|auth)[\"':=\s]+)[\"']?[A-Za-z0-9_\-\.]{6,}[\"']?", re.IGNORECASE), r"\1***REDACTED***"),
    # Linear API key: lin_api_...
    (re.compile(r"lin_api_[A-Za-z0-9]+", re.IGNORECASE), "***REDACTED***"),
    # GitHub token: ghp_..., gho_..., github_pat_...
    (re.compile(r"(ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]+", re.IGNORECASE), "***REDACTED***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{30,}", re.IGNORECASE), "***REDACTED***"),
    # Google API Key: AIza...
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}", re.IGNORECASE), "***REDACTED***"),
    # Groq API key: gsk_...
    (re.compile(r"gsk_[A-Za-z0-9]{20,}", re.IGNORECASE), "***REDACTED***"),
]


def mask_sensitive_data(text: str) -> str:
    """Che giấu các thông tin nhạy cảm trong chuỗi văn bản."""
    if not isinstance(text, str):
        return text
    masked = text
    for pattern, replacement in SECRET_PATTERNS:
        masked = pattern.sub(replacement, masked)
    return masked


class SecretMaskingFilter(logging.Filter):
    """Logging Filter tự động che giấu secret trong message và arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_sensitive_data(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: mask_sensitive_data(str(v)) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, (tuple, list)):
                record.args = tuple(mask_sensitive_data(str(a)) if isinstance(a, str) else a for a in record.args)
        return True


class StructuredJsonFormatter(logging.Formatter):
    """Formatter chuyển đổi LogRecord thành JSON duy nhất một dòng."""

    def format(self, record: logging.LogRecord) -> str:
        # Lấy correlation IDs từ contextvars hoặc từ record extra
        run_id = getattr(record, "run_id", None) or current_run_id.get()
        task_id = getattr(record, "task_id", None) or current_task_id.get()
        agent_id = getattr(record, "agent_id", None) or current_agent_id.get()

        message = record.getMessage()
        message = mask_sensitive_data(message)

        log_entry: Dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "caller": f"{record.filename}:{record.lineno}",
        }

        if run_id:
            log_entry["run_id"] = run_id
        if task_id:
            log_entry["task_id"] = task_id
        if agent_id:
            log_entry["agent_id"] = agent_id

        # Nếu có exception info
        if record.exc_info:
            log_entry["exception"] = mask_sensitive_data(self.formatException(record.exc_info))

        return json.dumps(log_entry, ensure_ascii=False)


def setup_structured_logging(
    level: int = logging.INFO,
    json_format: bool = True,
    logger_name: Optional[str] = None,
) -> logging.Logger:
    """Khởi tạo cấu hình logging chuẩn hóa cho hệ thống."""
    target_logger = logging.getLogger(logger_name)
    target_logger.setLevel(level)

    # Tránh gắn handler trùng lặp
    for h in list(target_logger.handlers):
        target_logger.removeHandler(h)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.addFilter(SecretMaskingFilter())

    if json_format:
        handler.setFormatter(StructuredJsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("[%(asctime)s] %(levelname)s [%(name)s] %(message)s")
        )

    target_logger.addHandler(handler)
    target_logger.propagate = False
    return target_logger
