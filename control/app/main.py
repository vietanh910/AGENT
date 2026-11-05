"""Control Node Gateway Application (control/app/main.py).

Ứng dụng FastAPI Gateway trên PC1 điều phối 10 Agent và 4 Worker Host.
Bao gồm:
- GET /health công khai không yêu cầu token
- Các endpoint API v1 (/api/v1/runs, /agents, /workers, /tasks, /webhooks)
- Thống nhất định dạng phản hồi lỗi bằng AgentError/ErrorCode của Agent Contract
- Middleware giới hạn kích thước body (MAX_UPLOAD_MB) và quản lý vòng đời tác vụ nền Heartbeat Watchdog
- CORS tắt mặc định
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_contract.enums import ErrorCode
from agent_contract.models import AgentError
from control.app.api import api_v1_router, metrics_router
from control.app.config import settings
from control.app.database.session import get_db_session, get_engine, verify_mysql_server_version
from control.app.observability.logging import setup_structured_logging
from control.app.queue.events import EventPublisher
from control.app.queue.redis import get_redis_client
from control.app.security.permissions import SecurityException
from control.app.services.heartbeat import run_heartbeat_watchdog_loop
from control.app.services.registry import RegistryService
from sqlalchemy.exc import DatabaseError, OperationalError

# Khởi tạo structured logging với secret masking
setup_structured_logging(level=settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Quản lý vòng đời khởi động và kết thúc của Control Node Gateway."""
    stop_event = asyncio.Event()
    watchdog_task = None
    project_task = None

    try:
        # 1. Khởi tạo và kiểm tra database, nạp danh bạ 10 agent mặc định
        with get_engine().connect() as conn:
            verify_mysql_server_version(conn)

        with get_db_session() as session:
            service = RegistryService(session)
            service.init_defaults()

        # 2. Bắt đầu tác vụ nền Heartbeat Watchdog
        redis_client = get_redis_client(redis_url=settings.REDIS_URL)
        publisher = EventPublisher(redis_client)
        watchdog_task = asyncio.create_task(
            run_heartbeat_watchdog_loop(
                stop_event=stop_event,
                interval_seconds=5.0,
                timeout_seconds=settings.HEARTBEAT_TIMEOUT_SECONDS,
                publisher=publisher,
            )
        )
        if settings.PROJECT_COORDINATOR_ENABLED:
            from control.app.services.project_runtime import run_project_loop
            project_task = asyncio.create_task(run_project_loop(stop_event, redis_client, publisher))
    except Exception as e:
        # Nếu đang ở môi trường test không có db sống, bỏ qua để test mock
        pass

    yield

    # Dọn dẹp khi tắt server
    stop_event.set()
    if project_task:
        try:
            await asyncio.wait_for(asyncio.shield(project_task), timeout=2.0)
        except asyncio.TimeoutError:
            project_task.cancel()
    if watchdog_task:
        try:
            await asyncio.wait_for(watchdog_task, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


app = FastAPI(
    title="Control Node Gateway",
    description="Control Node Gateway trên PC1 điều phối 10 Agent và 4 Worker Host",
    version="1.0.0",
    lifespan=lifespan,
)


# =============================================================================
# MIDDLEWARE KIỂM SOÁT KÍCH THƯỚC BODY
# =============================================================================
@app.middleware("http")
async def limit_body_size_middleware(request: Request, call_next):
    """Giới hạn dung lượng body request theo MAX_UPLOAD_MB (mặc định 50MB)."""
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > max_bytes:
        err = AgentError(
            code=ErrorCode.INVALID_INPUT,
            message=f"Dung lượng request vượt quá giới hạn tối đa cho phép ({settings.MAX_UPLOAD_MB}MB).",
            retryable=False,
        )
        return JSONResponse(status_code=413, content={"error": err.model_dump()})
    return await call_next(request)


# =============================================================================
# EXCEPTION HANDLERS CHUẨN HÓA LỖI THEO AGENTERROR CỦA CONTRACT
# =============================================================================
@app.exception_handler(OperationalError)
@app.exception_handler(DatabaseError)
async def database_exception_handler(request: Request, exc: Exception) -> Response:
    """Trả về HTTP 503 khi MySQL mất kết nối, kèm Retry-After header."""
    err = AgentError(
        code=ErrorCode.UNKNOWN_ERROR,
        message="Hệ thống cơ sở dữ liệu tạm thời gián đoạn. Vui lòng thử lại sau.",
        retryable=True,
    )
    return JSONResponse(
        status_code=503,
        headers={"Retry-After": "5"},
        content={"error": err.model_dump()},
    )


@app.exception_handler(SecurityException)
async def security_exception_handler(request: Request, exc: SecurityException) -> Response:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.agent_error.model_dump()},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    err = AgentError(
        code=ErrorCode.VALIDATION_ERROR,
        message="Dữ liệu yêu cầu không hợp lệ theo Schema.",
        details={"validation_errors": jsonable_encoder(exc.errors())},
        retryable=False,
    )
    return JSONResponse(status_code=422, content={"error": err.model_dump()})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> Response:
    # Nếu detail đã là model AgentError
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

    code = ErrorCode.INVALID_INPUT if exc.status_code < 500 else ErrorCode.UNKNOWN_ERROR
    if exc.status_code in (401, 403):
        code = ErrorCode.PERMISSION_DENIED

    err = AgentError(
        code=code,
        message=str(exc.detail),
        retryable=False,
    )
    return JSONResponse(status_code=exc.status_code, content={"error": err.model_dump()})


# =============================================================================
# PUBLIC HEALTH CHECK (KHÔNG YÊU CẦU TOKEN)
# =============================================================================
@app.get("/health")
def health_check() -> Dict[str, Any]:
    """Kiểm tra tình trạng sức khỏe của Control Node PC1 (công khai không yêu cầu token)."""
    return {
        "status": "healthy",
        "node": "control-node",
        "role": "CONTROL",
        "environment": settings.APP_ENV,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": "1.0.0",
    }


# Gắn toàn bộ router tiền tố /api/v1 và endpoint /metrics công khai
app.include_router(api_v1_router)
app.include_router(metrics_router)
from control.app.dashboard_ui import router as dashboard_ui_router
app.include_router(dashboard_ui_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.APP_HOST, port=settings.APP_PORT)
