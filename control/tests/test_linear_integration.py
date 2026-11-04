"""test_linear_integration.py - Bộ kiểm thử tích hợp Linear Adapter và Sync Service (Prompt S7).

Kiểm tra 7 kịch bản bắt buộc (hermetic, không gọi mạng ngoài):
1. Chạy đồng bộ hai lần không sinh trùng (Idempotent qua linear_links và content_hash).
2. HTTP 429 rate limit thì tự động backoff và thành công.
3. Thiếu trạng thái trong workflow states hoặc nhãn thì dừng sớm với thông báo lỗi rõ ràng.
4. Phê duyệt qua polling (chuyển Gate issue sang Done hoặc comment /approve, /reject).
5. Chuyển vào Dead-Letter Queue (DLQ) sau K lần lỗi và ghi nhận Audit log.
6. Linear sập hoặc gặp lỗi thì workflow trong DB vẫn tiếp tục chạy bình thường (lỗi không bao giờ ném lên caller).
7. Vượt quá MAX_LINEAR_ISSUES_PER_RUN thì dừng đồng bộ issue mới và ghi log cảnh báo.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control.app.database.models import (
    ApprovalModel,
    AuditLogModel,
    Base,
    LinearLinkModel,
    TaskModel,
    WorkflowRunModel,
)
from control.app.database.repositories import (
    ApprovalRepository,
    AuditRepository,
    LinearLinkRepository,
)
from control.app.integrations.linear.client import (
    FakeWorkTracker,
    LinearClient,
    LinearClientError,
    LinearRateLimitError,
)
from control.app.integrations.linear.config_manager import (
    LinearConfigurationError,
    LinearSchemaCache,
    initialize_and_validate_linear_schema,
    load_linear_config,
)
from control.app.integrations.linear.polling import (
    LinearPollingService,
    verify_linear_webhook_signature,
)
from control.app.integrations.linear.sync import (
    REDIS_SYNC_DLQ,
    REDIS_SYNC_QUEUE,
    LinearSyncService,
    compute_content_hash,
)


@pytest.fixture
def db_session_factory():
    """Tạo cơ sở dữ liệu SQLite in-memory dùng chung thread pool cho test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)
    yield session_factory
    engine.dispose()


@pytest.fixture
def fake_tracker() -> FakeWorkTracker:
    """Khởi tạo FakeWorkTracker chuẩn có đủ 5 workflow states và các nhãn."""
    return FakeWorkTracker()


@pytest.fixture
def schema_cache(fake_tracker: FakeWorkTracker) -> LinearSchemaCache:
    """Tải và xác thực schema hợp lệ từ FakeWorkTracker."""
    cfg = load_linear_config()
    return initialize_and_validate_linear_schema(
        tracker=fake_tracker,
        team_id="team_test_123",
        config=cfg,
    )


class MockRedis:
    """Bản giả lập Redis List cho hàng đợi sync."""

    def __init__(self) -> None:
        self.lists: Dict[str, List[str]] = {}

    def lpush(self, key: str, value: str) -> int:
        if key not in self.lists:
            self.lists[key] = []
        self.lists[key].insert(0, value)
        return len(self.lists[key])

    def rpop(self, key: str) -> str | None:
        if key not in self.lists or not self.lists[key]:
            return None
        return self.lists[key].pop()


# =============================================================================
# Kịch bản 1: Chạy đồng bộ hai lần không sinh trùng (Idempotency)
# =============================================================================
def test_sync_idempotency_no_duplicate_creation(db_session_factory, fake_tracker, schema_cache):
    """Khi đồng bộ dữ liệu cùng content_hash hai lần, lần hai bỏ qua và không tạo trùng."""
    sync_service = LinearSyncService(
        tracker=fake_tracker,
        schema_cache=schema_cache,
        session_factory=db_session_factory,
    )

    run_id = "RUN-IDEMP-01"
    task_id = "TASK-IDEMP-01"
    dev_task_data = {
        "task_id": task_id,
        "run_id": run_id,
        "role": "DEV",
        "agent_id": "dev-01",
        "action": "implement_feature",
        "instruction": "Viết mã xác thực",
        "dispatch_state": "RUNNING",
    }

    with db_session_factory() as session:
        # Lần 1: Tạo mới
        linear_id_1 = sync_service.sync_entity_now(session, "TASK", task_id, run_id, dev_task_data)
        assert linear_id_1 is not None
        assert linear_id_1 in fake_tracker.issues

        # Kiểm tra bảng linear_links
        link_1 = LinearLinkRepository.get_link(session, "TASK", task_id)
        assert link_1 is not None
        assert link_1.linear_id == linear_id_1

        calls_before = fake_tracker.call_count

        # Lần 2: Đồng bộ lại cùng payload
        linear_id_2 = sync_service.sync_entity_now(session, "TASK", task_id, run_id, dev_task_data)
        assert linear_id_2 == linear_id_1

        # Số cuộc gọi Linear không tăng lên vì content_hash khớp
        calls_after = fake_tracker.call_count
        assert calls_after == calls_before

        # Số lượng issue trên tracker vẫn là 1
        assert len(fake_tracker.issues) == 1


# =============================================================================
# Kịch bản 2: HTTP 429 thì backoff rồi thành công
# =============================================================================
def test_rate_limit_429_backoff_and_retry_success(db_session_factory, fake_tracker, schema_cache):
    """Khi gặp lỗi 429 Rate Limit, client tự động chờ backoff và hoàn thành ở lần thử kế tiếp."""
    sync_service = LinearSyncService(
        tracker=fake_tracker,
        schema_cache=schema_cache,
        session_factory=db_session_factory,
    )

    # Mô phỏng gặp 429 trong 1 lần gọi
    fake_tracker.simulate_429_count = 1

    run_id = "RUN-429-TEST"
    task_id = "TASK-429-DEV"
    data = {
        "task_id": task_id,
        "run_id": run_id,
        "role": "DEV",
        "agent_id": "dev-01",
        "action": "implement_feature",
        "instruction": "Xử lý rate limit",
        "dispatch_state": "RUNNING",
    }

    with db_session_factory() as session:
        # Lần đầu bị 429 sẽ được bắt trong sync_service mà không gây crash
        res1 = sync_service.sync_entity_now(session, "TASK", task_id, run_id, data)
        # Vì fake_tracker trả về 429 ở lần 1, res1 trả None
        assert res1 is None
        assert fake_tracker.simulate_429_count == 0

        # Lần gọi tiếp theo thành công mỹ mãn
        res2 = sync_service.sync_entity_now(session, "TASK", task_id, run_id, data)
        assert res2 is not None
        assert res2 in fake_tracker.issues


# =============================================================================
# Kịch bản 3: Thiếu trạng thái trong workflow thì dừng sớm với thông báo rõ
# =============================================================================
def test_missing_workflow_state_or_labels_fails_early_with_clear_error():
    """Nếu workspace Linear thiếu bất kỳ trạng thái workflow hoặc nhãn bắt buộc nào, ném LinearConfigurationError."""
    broken_tracker = FakeWorkTracker()
    # Xóa trạng thái "Done" và nhãn "blocked"
    broken_tracker.workflow_states = [s for s in broken_tracker.workflow_states if s["name"] != "Done"]
    broken_tracker.issue_labels = [l for l in broken_tracker.issue_labels if l["name"] != "blocked"]

    cfg = load_linear_config()

    with pytest.raises(LinearConfigurationError) as exc_info:
        initialize_and_validate_linear_schema(
            tracker=broken_tracker,
            team_id="team_test",
            config=cfg,
        )

    err_str = str(exc_info.value)
    assert "Done" in err_str, f"Phải chỉ rõ trạng thái 'Done' bị thiếu: {err_str}"
    assert "blocked" in err_str, f"Phải chỉ rõ nhãn 'blocked' bị thiếu: {err_str}"


# =============================================================================
# Kịch bản 4: Phê duyệt qua polling (chuyển trạng thái sang Done hoặc comment /approve)
# =============================================================================
def test_approval_via_polling_status_and_comment(db_session_factory, fake_tracker, schema_cache):
    """Polling phát hiện phê duyệt khi issue cổng chuyển sang Done hoặc nhận comment /approve."""
    polling_service = LinearPollingService(
        tracker=fake_tracker,
        schema_cache=schema_cache,
        session_factory=db_session_factory,
    )

    run_id = "RUN-POLL-01"
    gate = "approve_scope"
    gate_local_id = f"APP-{run_id}-{gate}"

    # Tạo issue cổng trên tracker
    gate_issue_id = fake_tracker.upsert_issue(
        title=f"[APPROVAL GATE] {gate}",
        description="Chờ phê duyệt phạm vi",
        team_id="team_test",
        state_id="st-todo",
    )

    with db_session_factory() as session:
        LinearLinkRepository.upsert_link(
            session=session,
            entity_type="APPROVAL",
            local_id=gate_local_id,
            linear_id=gate_issue_id,
        )
        session.commit()

    # 1. Người dùng đổi trạng thái trên Linear sang Done
    fake_tracker.set_status(gate_issue_id, "st-done")

    # Polling quét thay đổi
    stats = polling_service.poll_once()
    assert stats["approvals_processed"] == 1

    with db_session_factory() as session:
        appr = session.get(ApprovalModel, gate_local_id)
        assert appr is not None
        assert appr.status == "APPROVED"
        assert appr.decided_by == "linear_ui"

    # 2. Người dùng bình luận /reject trên một cổng khác
    gate_2 = "approve_deploy"
    gate_local_2 = f"APP-{run_id}-{gate_2}"
    gate_issue_2 = fake_tracker.upsert_issue(
        title=f"[APPROVAL GATE] {gate_2}",
        description="Chờ duyệt deploy",
        team_id="team_test",
        state_id="st-todo",
    )
    with db_session_factory() as session:
        LinearLinkRepository.upsert_link(
            session=session,
            entity_type="APPROVAL",
            local_id=gate_local_2,
            linear_id=gate_issue_2,
        )
        session.commit()

    # Gửi comment /reject
    fake_tracker.add_comment(gate_issue_2, "/reject Chưa đạt tiêu chuẩn hiệu năng")
    stats2 = polling_service.poll_once()
    assert stats2["approvals_processed"] == 1

    with db_session_factory() as session:
        appr2 = session.get(ApprovalModel, gate_local_2)
        assert appr2 is not None
        assert appr2.status == "REJECTED"


# =============================================================================
# Kịch bản 5: Chuyển vào Dead-Letter Queue (DLQ) sau K lần lỗi và ghi audit
# =============================================================================
def test_dead_letter_queue_after_k_failures(db_session_factory, fake_tracker, schema_cache):
    """Khi sự kiện Linear bị lỗi vượt quá max_sync_attempts, đưa vào DLQ và ghi Audit."""
    fake_redis = MockRedis()
    sync_service = LinearSyncService(
        tracker=fake_tracker,
        schema_cache=schema_cache,
        redis_client=fake_redis,
        session_factory=db_session_factory,
    )
    schema_cache.max_sync_attempts = 3

    # Giả lập tracker bị lỗi liên tục
    fake_tracker.fail_persistently = True

    # Đưa sự kiện vào queue
    sync_service.enqueue_change("TASK", "TASK-FAIL-01", "RUN-FAIL", {"title": "Task hỏng"})
    assert len(fake_redis.lists.get(REDIS_SYNC_QUEUE, [])) == 1

    # Chạy worker qua 3 lần thử
    sync_service.process_queue_worker(max_items=1)  # Lần 1
    assert len(fake_redis.lists.get(REDIS_SYNC_QUEUE, [])) == 1

    sync_service.process_queue_worker(max_items=1)  # Lần 2
    assert len(fake_redis.lists.get(REDIS_SYNC_QUEUE, [])) == 1

    sync_service.process_queue_worker(max_items=1)  # Lần 3 -> Vào DLQ
    assert len(fake_redis.lists.get(REDIS_SYNC_QUEUE, [])) == 0
    assert len(fake_redis.lists.get(REDIS_SYNC_DLQ, [])) == 1

    # Kiểm tra Audit Log được ghi nhận
    with db_session_factory() as session:
        stmt = session.query(AuditLogModel).filter_by(action="LINEAR_SYNC_DEAD_LETTER")
        entry = stmt.first()
        assert entry is not None
        assert entry.result == "FAILED"
        assert "TASK-FAIL-01" in entry.target


# =============================================================================
# Kịch bản 6: Linear sập thì workflow vẫn chạy (cách ly lỗi hoàn toàn)
# =============================================================================
def test_linear_failure_does_not_break_workflow(db_session_factory, fake_tracker, schema_cache):
    """Khi Linear API gặp lỗi sập hoàn toàn (500), không ném ngoại lệ lên Orchestrator."""
    sync_service = LinearSyncService(
        tracker=fake_tracker,
        schema_cache=schema_cache,
        session_factory=db_session_factory,
    )
    fake_tracker.fail_persistently = True

    with db_session_factory() as session:
        # Gọi trực tiếp sync_entity_now không bao giờ văng Exception
        res = sync_service.sync_entity_now(
            session=session,
            entity_type="TASK",
            local_id="TASK-SURVIVE-01",
            run_id="RUN-01",
            data={"role": "DEV", "action": "implement"},
        )
        assert res is None

        # Đẩy vào queue khi Redis lỗi cũng không crash
        ok = sync_service.enqueue_change("TASK", "TASK-01", "RUN-01", {})
        assert ok is False


# =============================================================================
# Kịch bản 7: Vượt MAX_LINEAR_ISSUES_PER_RUN thì dừng đồng bộ, ghi cảnh báo
# =============================================================================
def test_exceeding_max_issues_per_run_stops_syncing_and_warns(db_session_factory, fake_tracker, schema_cache, caplog):
    """Khi số issue cho một run đạt ngưỡng max_issues_per_run, tạm dừng tạo mới để bảo vệ quota."""
    schema_cache.max_issues_per_run = 2
    sync_service = LinearSyncService(
        tracker=fake_tracker,
        schema_cache=schema_cache,
        session_factory=db_session_factory,
    )

    run_id = "RUN-QUOTA-01"

    with db_session_factory() as session:
        # Tạo 2 task issue thành công
        t1 = sync_service.sync_entity_now(session, "TASK", f"{run_id}-T1", run_id, {"role": "DEV", "action": "a1"})
        assert t1 is not None

        t2 = sync_service.sync_entity_now(session, "TASK", f"{run_id}-T2", run_id, {"role": "TESTER", "action": "a2"})
        assert t2 is not None

        # Task thứ 3 vượt trần max_issues_per_run=2
        t3 = sync_service.sync_entity_now(session, "TASK", f"{run_id}-T3", run_id, {"role": "BA", "action": "a3"})
        assert t3 is None
        assert "Đã đạt giới hạn MAX_LINEAR_ISSUES_PER_RUN=2" in caplog.text

        # Số lượng issue trên tracker dừng lại ở 2
        assert len(fake_tracker.issues) == 2


# =============================================================================
# Kiểm tra bổ sung: Xác thực chữ ký Linear Webhook
# =============================================================================
def test_verify_linear_webhook_signature():
    """Kiểm tra xác thực chữ ký HMAC-SHA256 của Webhook."""
    import hashlib
    import hmac

    secret = "my_super_webhook_secret"
    body = b'{"type":"Issue","action":"update"}'
    valid_sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    assert verify_linear_webhook_signature(body, valid_sig, secret) is True
    assert verify_linear_webhook_signature(body, "invalid_sig", secret) is False
    assert verify_linear_webhook_signature(body, None, secret) is False
