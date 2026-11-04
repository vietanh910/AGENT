"""control/tests/test_operational_resilience.py - Bộ kiểm thử khả năng vận hành dài hạn (Prompt S11).

Kiểm thử:
1. Structured JSON logging & secret masking (Bearer, tokens, keys).
2. Audit log Merkle Hash Chain & tamper detection.
3. ActionPolicy enforcement (Dispatcher & ResultHandler).
4. Worker token attempting to call ADMIN endpoint (403 Forbidden + ADMIN_ACCESS_DENIED audit).
5. Capped backoff delay calculation.
6. Run retry budget exhaustion -> BLOCKED.
7. Poison task isolation -> BLOCKED.
8. GET /metrics Prometheus exposition format.
9. Multi-run concurrency limit (MAX_CONCURRENT_RUNS).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent_contract.enums import AgentRole, ErrorCode, Priority, TaskStatus
from agent_contract.models import AgentError, TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus
from control.app.api.dependencies import get_db, get_redis
from control.app.config import settings
from control.app.database.models import (
    AgentModel,
    AuditLogModel,
    Base,
    TaskModel,
    WorkerModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import AuditRepository, TaskRepository, WorkflowRunRepository
from control.app.main import app
from control.app.observability.audit import AuditService
from control.app.observability.logging import (
    LogContext,
    SecretMaskingFilter,
    StructuredJsonFormatter,
    mask_sensitive_data,
)
from control.app.orchestrator.guards import WorkflowGuards
from control.app.security.authentication import AdminPrincipal, WorkerPrincipal
from control.app.security.permissions import ActionPolicy, PermissionDeniedException
from control.app.services.circuit_breaker import CircuitBreakerRegistry
from control.app.services.dispatcher import DispatcherService
from control.app.services.result_handler import ResultHandler
from control.app.services.task_manager import TaskManager
from control.app.services.worker_client import WorkerClient


# =============================================================================
# FIXTURES
# =============================================================================

@pytest.fixture
def sqlite_engine():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def db_session(sqlite_engine) -> Generator[Session, None, None]:
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=sqlite_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_client(db_session: Session):
    def override_get_db():
        yield db_session

    def override_get_redis():
        return None

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()


# =============================================================================
# 1. STRUCTURED LOGGING & SECRET MASKING
# =============================================================================

def test_secret_masking_tokens():
    """Kiểm tra bộ lọc che giấu bí mật cho các loại token nhạy cảm."""
    raw_text = (
        "Authorization: Bearer my_secret_token_12345, "
        "Linear: lin_api_fake_dummy_test_token_for_masking_12345, "
        "GitHub: ghp_1234567890abcdef1234567890abcdef123456, "
        "Google: AIzaSyD1234567890abcdefghijklmnopqrstuvw"
    )
    masked = mask_sensitive_data(raw_text)
    assert "my_secret_token_12345" not in masked
    assert "lin_api_fake_dummy_test_token_for_masking_12345" not in masked
    assert "ghp_1234567890abcdef1234567890abcdef123456" not in masked
    assert "AIzaSyD1234567890abcdefghijklmnopqrstuvw" not in masked
    assert "***REDACTED***" in masked


def test_structured_json_formatter_correlation():
    """Kiểm tra StructuredJsonFormatter định dạng đúng JSON kèm correlation id."""
    formatter = StructuredJsonFormatter()
    logger = logging.getLogger("test_logger")
    record = logger.makeRecord(
        name="test_logger",
        level=logging.INFO,
        fn="test_file.py",
        lno=42,
        msg="Test message with lin_api_secret123456789",
        args=(),
        exc_info=None,
    )

    with LogContext(run_id="RUN-123", task_id="TASK-456", agent_id="dev-01"):
        formatted = formatter.format(record)

    data = json.loads(formatted)
    assert "***REDACTED***" in data["message"]
    assert data["run_id"] == "RUN-123"
    assert data["task_id"] == "TASK-456"
    assert data["agent_id"] == "dev-01"


# =============================================================================
# 2. AUDIT LOG MERKLE HASH CHAIN & TAMPER DETECTION
# =============================================================================

def test_audit_log_hash_chain_tamper_detection(db_session: Session):
    """Kiểm tra Merkle hash chain của audit log phát hiện được sửa đổi dữ liệu."""
    AuditService.log_task_dispatched(db_session, run_id="RUN-1", task_id="T-1", worker_id="W-1", agent_id="dev-01", attempt=1)
    AuditService.log_task_result(db_session, run_id="RUN-1", task_id="T-1", agent_id="dev-01", status="COMPLETED", worker_id="W-1")
    AuditService.log_run_status(db_session, run_id="RUN-1", old_status="RUNNING", new_status="COMPLETED")

    # Kiểm tra chuỗi nguyên vẹn
    valid, broken_id, reason = AuditService.verify_chain_integrity(db_session)
    assert valid is True
    assert broken_id is None

    # Giả lập kẻ tấn công sửa lén kết quả bản ghi thứ 2 trong database
    records = db_session.query(AuditLogModel).order_by(AuditLogModel.id.asc()).all()
    records[1].result = "TAMPERED_RESULT"
    db_session.commit()

    # Kiểm tra phát hiện gian lận
    valid, broken_id, reason = AuditService.verify_chain_integrity(db_session)
    assert valid is False
    assert broken_id == records[1].id


# =============================================================================
# 3. ACTION POLICY ENFORCEMENT
# =============================================================================

def test_action_policy_allowed_and_forbidden():
    """Kiểm tra ActionPolicy đúng vai trò hợp lệ và từ chối hành động sai."""
    assert ActionPolicy.is_action_allowed(AgentRole.DEV, "implement_project") is True
    assert ActionPolicy.is_action_allowed(AgentRole.DEV, "implement_feature") is True
    assert ActionPolicy.is_action_allowed(AgentRole.DEV, "deploy") is False  # DEV không được deploy

    assert ActionPolicy.is_action_allowed(AgentRole.DEVOPS, "deploy") is True
    assert ActionPolicy.is_action_allowed(AgentRole.DEVOPS, "create_plan") is False  # DevOps không làm plan

    with pytest.raises(PermissionDeniedException):
        ActionPolicy.enforce_action(AgentRole.DEV, "deploy")


def test_dispatcher_action_policy_violation(db_session: Session):
    """Dispatcher phát hiện vi phạm ActionPolicy thì FAILED task ngay và không dispatch."""
    run = WorkflowRunModel(run_id="RUN-POLICY-1", project_id="PRJ-1", brief="Policy test 1", status=RunStatus.RUNNING.value)
    db_session.add(run)

    # DEV thực hiện action 'deploy' (vi phạm ActionPolicy)
    task = TaskModel(
        task_id="T-INVALID-ROLE",
        run_id="RUN-POLICY-1",
        role="DEV",
        action="deploy",
        instruction="Deploy staging",
        agent_id="dev-01",
        dispatch_state=DispatchState.QUEUED.value,
        attempts=0,
        max_attempts=3,
        timeout_seconds=60,
    )
    db_session.add(task)
    db_session.commit()

    dispatcher = DispatcherService(session_factory=lambda: db_session)
    dispatched = dispatcher.dispatch_single_task(db_session, task)

    assert dispatched is False
    db_session.refresh(task)
    assert task.dispatch_state == DispatchState.FAILED.value
    assert task.error["code"] == ErrorCode.PERMISSION_DENIED.value


def test_result_handler_action_policy_violation(db_session: Session):
    """ResultHandler từ chối nhận kết quả cho task vi phạm ActionPolicy."""
    run = WorkflowRunModel(run_id="RUN-POLICY-2", project_id="PRJ-2", brief="Policy test 2", status=RunStatus.RUNNING.value)
    db_session.add(run)

    task = TaskModel(
        task_id="T-RES-POLICY",
        run_id="RUN-POLICY-2",
        role="TESTER",
        action="deploy",  # TESTER không được deploy
        instruction="Do deploy",
        agent_id="tester-01",
        dispatch_state=DispatchState.ASSIGNED.value,
        attempts=1,
        max_attempts=3,
        timeout_seconds=60,
    )
    db_session.add(task)
    db_session.commit()

    worker = WorkerPrincipal(worker_id="w-1", machine_id="PC4", allowed_agents=["tester-01"])
    result = TaskResult(
        task_id="T-RES-POLICY",
        agent_id="tester-01",
        role=AgentRole.TESTER,
        status=TaskStatus.COMPLETED,
        summary="Done deploy",
        created_at=utc_now(),
    )

    with pytest.raises(PermissionDeniedException):
        ResultHandler.handle_result(db_session, "T-RES-POLICY", result, worker)


# =============================================================================
# 4. WORKER CALLING ADMIN ENDPOINT RESTRICTION
# =============================================================================

def test_worker_calling_admin_endpoint_forbidden(test_client: TestClient, db_session: Session):
    """Token WORKER bị cấm gọi endpoint ADMIN (403 + ghi audit log ADMIN_ACCESS_DENIED)."""
    # Gửi request với Worker Token hợp lệ trong control/workers.yaml
    headers = {"Authorization": "Bearer worker_pc4_secret_token_change_me"}
    resp = test_client.post("/api/v1/runs", json={"prompt": "Build an app"}, headers=headers)

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == ErrorCode.PERMISSION_DENIED.value

    # Kiểm tra Audit Log có ghi nhận ADMIN_ACCESS_DENIED
    audit = db_session.query(AuditLogModel).filter(AuditLogModel.action == "ADMIN_ACCESS_DENIED").first()
    assert audit is not None
    assert audit.result == "FORBIDDEN_403"
    assert audit.actor == "worker-pc4"


# =============================================================================
# 5. CAPPED BACKOFF & RETRY BUDGET & POISON TASK ISOLATION
# =============================================================================

def test_capped_backoff_delay():
    """Kiểm tra thời gian chờ backoff lũy thừa có chặn trần tối đa 60s."""
    assert TaskManager.calculate_backoff_delay(1, base_seconds=2.0, max_seconds=60.0) == 2.0
    assert TaskManager.calculate_backoff_delay(2, base_seconds=2.0, max_seconds=60.0) == 4.0
    assert TaskManager.calculate_backoff_delay(3, base_seconds=2.0, max_seconds=60.0) == 8.0
    assert TaskManager.calculate_backoff_delay(5, base_seconds=2.0, max_seconds=60.0) == 32.0
    assert TaskManager.calculate_backoff_delay(6, base_seconds=2.0, max_seconds=60.0) == 60.0  # Capped at 60s
    assert TaskManager.calculate_backoff_delay(10, base_seconds=2.0, max_seconds=60.0) == 60.0


def test_retry_budget_exhaustion_isolates_to_blocked(db_session: Session):
    """Vượt quá ngân sách retry của run (MAX_RUN_RETRIES) -> cách ly vào BLOCKED."""
    run_id = "RUN-BUDGET-EXHAUST"
    run = WorkflowRunModel(run_id=run_id, project_id="PRJ-BUDGET", brief="Budget test", status=RunStatus.RUNNING.value)
    db_session.add(run)

    # Tạo các task đã tiêu tốn tổng cộng 16 attempts
    for i in range(5):
        t = TaskModel(
            task_id=f"T-PREV-{i}",
            run_id=run_id,
            role="DEV",
            action="implement_feature",
            instruction=f"Feature {i}",
            agent_id="dev-01",
            dispatch_state=DispatchState.COMPLETED.value,
            attempts=4,  # 3 retries each x 5 = 15 retries
            max_attempts=5,
            timeout_seconds=60,
        )
        db_session.add(t)

    # Task hiện tại gặp lỗi retryable
    current_task = TaskModel(
        task_id="T-BUDGET-FAIL",
        run_id=run_id,
        role="DEV",
        action="implement_project",
        instruction="Skeleton",
        agent_id="dev-01",
        dispatch_state=DispatchState.ASSIGNED.value,
        attempts=2,  # Thêm retry nữa sẽ vượt ngân sách 15
        max_attempts=5,
        timeout_seconds=60,
    )
    db_session.add(current_task)
    db_session.commit()

    worker = WorkerPrincipal(worker_id="w-dev", machine_id="PC4", allowed_agents=["dev-01"])
    result = TaskResult(
        task_id="T-BUDGET-FAIL",
        agent_id="dev-01",
        role=AgentRole.DEV,
        status=TaskStatus.FAILED,
        summary="LLM failure",
        error=AgentError(code=ErrorCode.LLM_ERROR, message="LLM timeout", retryable=True),
        created_at=utc_now(),
    )

    res = ResultHandler.handle_result(db_session, "T-BUDGET-FAIL", result, worker)
    assert res["dispatch_state"] == DispatchState.BLOCKED.value

    db_session.refresh(current_task)
    assert current_task.dispatch_state == DispatchState.BLOCKED.value
    assert "ngân sách retry" in current_task.error["message"]


def test_poison_task_isolated_to_blocked(db_session: Session):
    """Task độc hại liên tục thất bại đạt max_attempts -> cách ly an toàn vào BLOCKED."""
    run_id = "RUN-POISON-1"
    run = WorkflowRunModel(run_id=run_id, project_id="PRJ-POISON", brief="Poison test", status=RunStatus.RUNNING.value)
    db_session.add(run)

    task = TaskModel(
        task_id="T-POISON-TASK",
        run_id=run_id,
        role="DEV",
        action="implement_project",
        instruction="Skeleton",
        agent_id="dev-01",
        dispatch_state=DispatchState.ASSIGNED.value,
        attempts=3,  # Đã chạy đủ 3/3 attempts
        max_attempts=3,
        timeout_seconds=60,
    )
    db_session.add(task)
    db_session.commit()

    worker = WorkerPrincipal(worker_id="w-dev", machine_id="PC4", allowed_agents=["dev-01"])
    result = TaskResult(
        task_id="T-POISON-TASK",
        agent_id="dev-01",
        role=AgentRole.DEV,
        status=TaskStatus.FAILED,
        summary="Fatal crash",
        error=AgentError(code=ErrorCode.UNKNOWN_ERROR, message="Fatal exception", retryable=True),
        created_at=utc_now(),
    )

    res = ResultHandler.handle_result(db_session, "T-POISON-TASK", result, worker)
    assert res["dispatch_state"] == DispatchState.BLOCKED.value

    db_session.refresh(task)
    assert task.dispatch_state == DispatchState.BLOCKED.value
    assert "POISON_TASK_DETECTED" in task.error["message"]


# =============================================================================
# 6. GET /METRICS PROMETHEUS FORMAT
# =============================================================================

def test_get_metrics_prometheus_format(test_client: TestClient, db_session: Session):
    """Kiểm tra GET /metrics trả về text/plain định dạng Prometheus."""
    resp = test_client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]

    body = resp.text
    assert "# HELP ai_team_tasks_total" in body
    assert "# TYPE ai_team_tasks_total gauge" in body
    assert 'ai_team_tasks_total{state="COMPLETED"}' in body
    assert "# HELP ai_team_workers_online" in body
    assert "ai_team_workers_online" in body
    assert "ai_team_linear_sync_queue_depth" in body
    assert "ai_team_runs_total" in body


# =============================================================================
# 7. MULTI-RUN CONCURRENCY LIMIT
# =============================================================================

def test_multi_run_concurrency_limit(test_client: TestClient, db_session: Session):
    """Kiểm tra giới hạn số run chạy đồng thời (MAX_CONCURRENT_RUNS = 3)."""
    headers = {"Authorization": f"Bearer {settings.ADMIN_TOKEN}"}

    # Tạo 3 runs đang hoạt động (PLANNING hoặc RUNNING)
    for i in range(settings.MAX_CONCURRENT_RUNS):
        run = WorkflowRunModel(
            run_id=f"RUN-CONC-{i}",
            project_id=f"PRJ-CONC-{i}",
            brief=f"Test run {i}",
            status=RunStatus.RUNNING.value,
        )
        db_session.add(run)
    db_session.commit()

    # Thử tạo run thứ 4 -> nhận HTTP 429
    resp = test_client.post(
        "/api/v1/runs",
        json={"prompt": "Fourth run exceeding concurrency limit"},
        headers=headers,
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == ErrorCode.DISPATCH_FAILED.value
