"""FastAPI Dependencies dùng chung cho Control Node Gateway."""

from __future__ import annotations

from typing import Generator, Optional

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from control.app.database.session import get_session_factory
from control.app.config import settings
from control.app.queue.events import EventPublisher
from control.app.queue.redis import get_redis_client
from control.app.security.authentication import (
    AdminPrincipal,
    Principal,
    WorkerPrincipal,
    authenticate_bearer,
    get_worker_secret_manager,
)
from control.app.security.permissions import (
    AuthenticationFailedException,
    PermissionDeniedException,
    enforce_admin,
    enforce_worker,
)


def get_db() -> Generator[Session, None, None]:
    """Cung cấp SQLAlchemy Session theo từng HTTP Request."""
    session_factory = get_session_factory()
    session: Session = session_factory()
    try:
        yield session
    finally:
        session.close()


def get_redis():
    """Cung cấp Redis client singleton."""
    return get_redis_client(redis_url=settings.REDIS_URL)


def get_publisher(redis_client=Depends(get_redis)) -> EventPublisher:
    """Cung cấp EventPublisher."""
    return EventPublisher(redis_client)


def get_current_principal(
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> Optional[Principal]:
    """Trích xuất và kiểm tra Bearer token từ header."""
    if not authorization:
        return None

    secret_mgr = get_worker_secret_manager(settings.WORKERS_FILE)
    return authenticate_bearer(
        auth_header=authorization,
        admin_token=settings.ADMIN_TOKEN,
        secret_manager=secret_mgr,
    )


def require_authenticated(
    principal: Optional[Principal] = Depends(get_current_principal),
) -> Principal:
    """Yêu cầu bắt buộc phải có Bearer token hợp lệ (Admin hoặc Worker)."""
    if principal is None:
        raise AuthenticationFailedException()
    return principal


def require_admin(
    request: Request = None,  # type: ignore[assignment]
    principal: Optional[Principal] = Depends(get_current_principal),
    session: Session = Depends(get_db),
) -> AdminPrincipal:
    """Yêu cầu bắt buộc quyền ADMIN. Token WORKER bị cấm gọi endpoint ADMIN (403 + ghi audit log)."""
    if principal is None:
        raise AuthenticationFailedException()
    if not isinstance(principal, AdminPrincipal):
        actor = getattr(principal, "worker_id", "unknown_worker")
        if session is not None and request is not None:
            try:
                from control.app.database.repositories import AuditRepository
                AuditRepository.record(
                    session=session,
                    actor=actor,
                    action="ADMIN_ACCESS_DENIED",
                    target=str(request.url.path),
                    result="FORBIDDEN_403",
                    details={
                        "role": getattr(principal, "role", "WORKER"),
                        "method": request.method,
                        "client": request.client.host if request.client else "unknown",
                    },
                )
                session.commit()
            except Exception:
                pass
        raise PermissionDeniedException(f"Token '{getattr(principal, 'role', 'WORKER')}' không được phép truy cập endpoint ADMIN.")
    return principal


def require_worker(
    principal: Optional[Principal] = Depends(get_current_principal),
) -> WorkerPrincipal:
    """Yêu cầu bắt buộc quyền WORKER."""
    return enforce_worker(principal)
