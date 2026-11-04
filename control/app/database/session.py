"""control/app/database/session.py - Quản lý kết nối và phiên làm việc MySQL.

Tuân thủ:
- SQLAlchemy 2.x, PyMySQL driver hỗ trợ caching_sha2_password (gói cryptography).
- Kiểm tra phiên bản MySQL server khi kết nối (yêu cầu tối thiểu 8.0.19, cấm 9.6 trở lên).
- Cấu hình session time_zone = '+00:00' (UTC) và connection pooling (pool_pre_ping, pool_recycle).
- Xử lý deadlock và lock wait timeout (mã 1213 và 1205) bằng cơ chế retry có giới hạn và backoff.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

from pymysql.err import OperationalError as PyMySQLOperationalError
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

logger = logging.getLogger(__name__)

# Base cho các ORM model
Base = declarative_base()

F = TypeVar("F", bound=Callable[..., Any])

MIN_MYSQL_VERSION = (8, 0, 19)
MAX_MYSQL_VERSION = (9, 6, 0)


class MySQLVersionError(RuntimeError):
    """Lỗi khi phiên bản MySQL server nằm ngoài phạm vi hỗ trợ."""


def parse_mysql_version_tuple(version_str: str) -> tuple[int, int, int]:
    """Phân tích chuỗi phiên bản MySQL (ví dụ: '8.4.13-commercial' hoặc '8.0.36') thành tuple."""
    match = re.search(r"^(\d+)\.(\d+)\.(\d+)", version_str)
    if not match:
        raise MySQLVersionError(f"Không thể phân tích chuỗi phiên bản MySQL: '{version_str}'")
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def verify_mysql_server_version(connection: Any) -> tuple[int, int, int]:
    """Kiểm tra phiên bản MySQL server: tối thiểu 8.0.19, dưới 9.6.0."""
    cursor = connection.cursor() if hasattr(connection, "cursor") else None
    if cursor:
        cursor.execute("SELECT VERSION()")
        raw_version = cursor.fetchone()[0]
    else:
        # Trường hợp SQLAlchemy connection object
        result = connection.execute(text("SELECT VERSION()"))
        raw_version = result.scalar()

    ver_tuple = parse_mysql_version_tuple(str(raw_version))
    if ver_tuple < MIN_MYSQL_VERSION:
        msg = (
            f"Phiên bản MySQL {ver_tuple} không đạt chuẩn tối thiểu {MIN_MYSQL_VERSION}. "
            "Hệ thống yêu cầu tối thiểu MySQL 8.0.19."
        )
        logger.critical(msg)
        raise MySQLVersionError(msg)

    if ver_tuple >= MAX_MYSQL_VERSION:
        msg = (
            f"Phiên bản MySQL {ver_tuple} vượt quá giới hạn an toàn {MAX_MYSQL_VERSION} "
            "(MySQL 9.6+ đã loại bỏ hàm MD5 trong generated column gây vỡ index). "
            "Vui lòng sử dụng MySQL 8.4 LTS."
        )
        logger.critical(msg)
        raise MySQLVersionError(msg)

    return ver_tuple


def create_db_engine(db_url: str | None = None) -> Engine:
    """Tạo SQLAlchemy Engine với kết nối MySQL tối ưu và an toàn."""
    if not db_url:
        db_url = os.getenv(
            "DATABASE_URL",
            "mysql+pymysql://ai_user:ai_password_change_me@127.0.0.1:3307/ai_team?charset=utf8mb4",
        )

    connect_args: dict[str, Any] = {
        "charset": "utf8mb4",
    }

    engine = create_engine(
        db_url,
        connect_args=connect_args,
        pool_pre_ping=True,      # Tự động ping kiểm tra kết nối sống trước khi mượn pool
        pool_recycle=3600,       # Tái tạo kết nối sau 1 giờ tránh MySQL timeout đóng kết nối
        pool_size=10,            # Số kết nối duy trì trong pool
        max_overflow=20,         # Số kết nối tối đa mở thêm khi cao tải
        future=True,
    )

    # Đảm bảo timezone của session là UTC cho mọi kết nối
    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET time_zone = '+00:00'")
        cursor.close()
        # Kiểm tra phiên bản server ngay khi mở kết nối đầu tiên
        verify_mysql_server_version(dbapi_connection)

    return engine


# Engine và SessionFactory toàn cục
_engine: Engine | None = None
_SessionFactory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """Lấy Engine singleton hoặc khởi tạo nếu chưa có."""
    global _engine, _SessionFactory
    if _engine is None:
        _engine = create_db_engine()
        _SessionFactory = sessionmaker(bind=_engine, autocommit=False, autoflush=False, expire_on_commit=False)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """Lấy SessionFactory singleton."""
    global _SessionFactory
    if _SessionFactory is None:
        get_engine()
    assert _SessionFactory is not None
    return _SessionFactory


@contextmanager
def get_db_session() -> Generator[Session, None, None]:
    """Context manager cung cấp Session với transaction tự động commit/rollback."""
    factory = get_session_factory()
    session: Session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_deadlock_or_lock_timeout(exc: Exception) -> bool:
    """Kiểm tra exception có phải do lỗi deadlock (1213) hoặc lock wait timeout (1205) của MySQL không."""
    if isinstance(exc, (DBAPIError, OperationalError, PyMySQLOperationalError)):
        if hasattr(exc, "args") and len(exc.args) > 0 and isinstance(exc.args[0], int):
            if exc.args[0] in (1213, 1205):
                return True
        orig = getattr(exc, "orig", None)
        if orig and hasattr(orig, "args") and len(orig.args) > 0 and isinstance(orig.args[0], int):
            if orig.args[0] in (1213, 1205):
                return True
        # Kiểm tra qua text lỗi
        err_msg = str(exc)
        if "1213" in err_msg or "1205" in err_msg or "Deadlock found" in err_msg or "Lock wait timeout" in err_msg:
            return True
    return False


def with_db_retry(
    max_retries: int = 3,
    base_delay: float = 0.05,
    max_delay: float = 0.5,
) -> Callable[[F], F]:
    """Decorator tự động thử lại khi gặp Deadlock (1213) hoặc Lock Wait Timeout (1205) với exponential backoff."""
    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            delay = base_delay
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as exc:
                    if is_deadlock_or_lock_timeout(exc) and attempt < max_retries:
                        last_exc = exc
                        logger.warning(
                            f"Phát hiện deadlock/lock timeout trong {func.__name__} (lần {attempt}/{max_retries}). "
                            f"Thử lại sau {delay:.3f}s... Lỗi: {exc}"
                        )
                        time.sleep(delay)
                        delay = min(delay * 2, max_delay)
                    else:
                        raise
            if last_exc:
                raise last_exc
        return wrapper  # type: ignore[return-value]
    return decorator
