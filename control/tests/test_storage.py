"""Kiểm thử tầng lưu trữ MySQL và Session/Repositories của Control Node (S3)."""

from __future__ import annotations

import os
import uuid
import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from pymysql.err import OperationalError

from agent_contract.system_models import RunStatus, DispatchState
from control.app.database.session import (
    MIN_MYSQL_VERSION,
    MAX_MYSQL_VERSION,
    MySQLVersionError,
    parse_mysql_version_tuple,
    verify_mysql_server_version,
    with_db_retry,
    get_session_factory,
    get_engine,
)
from control.app.database.repositories import (
    VALID_DISPATCH_TRANSITIONS,
    validate_dispatch_transition,
    InvalidStateTransitionError,
    VersionConflictError,
    TaskNotFoundError,
    TaskRepository,
    WorkflowRunRepository,
    WorkerRepository,
    AgentRepository,
    ArtifactRepository,
    ApprovalRepository,
    AuditRepository,
    LinearLinkRepository,
)
from control.app.database.checkpoint import (
    MemoryCheckpointStore,
    MySQLCheckpointStore,
)


# =============================================================================
# 1. TEST PHIÊN BẢN MYSQL
# =============================================================================
class TestMySQLVersionValidation:
    """Kiểm tra điều kiện phiên bản MySQL (8.0.19 <= v < 9.6.0)."""

    def test_parse_mysql_version_tuple(self) -> None:
        assert parse_mysql_version_tuple("8.4.13") == (8, 4, 13)
        assert parse_mysql_version_tuple("8.0.19-commercial") == (8, 0, 19)
        assert parse_mysql_version_tuple("8.4.0-LTS-community") == (8, 4, 0)
        with pytest.raises(MySQLVersionError):
            parse_mysql_version_tuple("invalid")

    def test_verify_valid_versions(self) -> None:
        for ver_str in ["8.0.19", "8.0.35", "8.4.0", "8.4.13", "9.0.0", "9.5.99"]:
            mock_conn = MagicMock()
            mock_conn.cursor.return_value.fetchone.return_value = (ver_str,)
            res = verify_mysql_server_version(mock_conn)
            assert res >= MIN_MYSQL_VERSION
            assert res < MAX_MYSQL_VERSION

    def test_verify_too_low_version(self) -> None:
        for ver_str in ["8.0.18", "8.0.0", "5.7.44"]:
            mock_conn = MagicMock()
            mock_conn.cursor.return_value.fetchone.return_value = (ver_str,)
            with pytest.raises(MySQLVersionError, match="không đạt chuẩn tối thiểu"):
                verify_mysql_server_version(mock_conn)

    def test_verify_too_high_version(self) -> None:
        for ver_str in ["9.6.0", "9.6.1", "10.0.0"]:
            mock_conn = MagicMock()
            mock_conn.cursor.return_value.fetchone.return_value = (ver_str,)
            with pytest.raises(MySQLVersionError, match="vượt quá giới hạn an toàn"):
                verify_mysql_server_version(mock_conn)


# =============================================================================
# 2. TEST RETRY VỚI DEADLOCK VÀ LOCK TIMEOUT
# =============================================================================
class TestDBRetryDecorator:
    """Kiểm thử decorator @with_db_retry xử lý mã lỗi 1213 và 1205."""

    def test_retry_on_deadlock_success(self) -> None:
        calls = 0

        @with_db_retry(max_retries=3, base_delay=0.01)
        def faulty_function() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                # Mã lỗi 1213: Deadlock found when trying to get lock
                raise OperationalError(1213, "Deadlock found")
            return "SUCCESS"

        result = faulty_function()
        assert result == "SUCCESS"
        assert calls == 3

    def test_retry_on_lock_timeout_success(self) -> None:
        calls = 0

        @with_db_retry(max_retries=3, base_delay=0.01)
        def lock_timeout_function() -> str:
            nonlocal calls
            calls += 1
            if calls < 2:
                # Mã lỗi 1205: Lock wait timeout exceeded
                raise OperationalError(1205, "Lock wait timeout exceeded")
            return "RECOVERED"

        result = lock_timeout_function()
        assert result == "RECOVERED"
        assert calls == 2

    def test_retry_exceeded_raises(self) -> None:
        @with_db_retry(max_retries=2, base_delay=0.01)
        def permanent_deadlock() -> None:
            raise OperationalError(1213, "Deadlock found")

        with pytest.raises(OperationalError) as exc_info:
            permanent_deadlock()
        assert exc_info.value.args[0] == 1213

    def test_other_operational_error_not_retried(self) -> None:
        calls = 0

        @with_db_retry(max_retries=3, base_delay=0.01)
        def other_error() -> None:
            nonlocal calls
            calls += 1
            # Mã lỗi 1045: Access denied
            raise OperationalError(1045, "Access denied")

        with pytest.raises(OperationalError):
            other_error()
        assert calls == 1


# =============================================================================
# 3. TEST CHUYỂN TRẠNG THÁI TASK (STATE MACHINE)
# =============================================================================
class TestDispatchStateTransitions:
    """Kiểm tra ma trận chuyển trạng thái hợp lệ và bất hợp lệ."""

    def test_valid_transitions(self) -> None:
        for current_state, allowed_targets in VALID_DISPATCH_TRANSITIONS.items():
            for target in allowed_targets:
                # Không được quăng exception
                validate_dispatch_transition(current_state, target)

    def test_invalid_transitions(self) -> None:
        invalid_pairs = [
            (DispatchState.QUEUED, DispatchState.COMPLETED),
            (DispatchState.QUEUED, DispatchState.RUNNING),
            (DispatchState.ASSIGNED, DispatchState.COMPLETED),
            (DispatchState.COMPLETED, DispatchState.RUNNING),
            (DispatchState.CANCELLED, DispatchState.ASSIGNED),
        ]
        for src, dst in invalid_pairs:
            with pytest.raises(InvalidStateTransitionError):
                validate_dispatch_transition(src, dst)


# =============================================================================
# 4. TEST CHECKPOINT STORE (MEMORY VÀ MYSQL)
# =============================================================================
class TestCheckpointStore:
    """Kiểm tra CheckpointStore memory và live MySQL."""

    def test_memory_checkpoint_store(self) -> None:
        store = MemoryCheckpointStore()
        thread_id = "test-mem-thread-1"
        data = {"count": 10, "status": "running"}

        cp_id = store.put_state(thread_id, data, metadata={"test": True})
        assert cp_id != ""

        loaded = store.get_state(thread_id)
        assert loaded is not None
        assert loaded.get("count") == 10
        assert loaded.get("status") == "running"

        # Thread không tồn tại
        assert store.get_state("non-existent-thread") is None

    @pytest.mark.skipif(
        not os.getenv("DATABASE_URL") and not os.path.exists("control/.env"),
        reason="Chỉ chạy khi có kết nối MySQL sống",
    )
    def test_mysql_checkpoint_store_live(self) -> None:
        store = MySQLCheckpointStore()
        try:
            store.setup()
            thread_id = f"test-mysql-thread-{uuid.uuid4().hex[:8]}"
            data = {"workflow_phase": "testing", "score": 99.5}

            cp_id = store.put_state(thread_id, data)
            assert cp_id != ""

            loaded = store.get_state(thread_id)
            assert loaded is not None
            assert loaded.get("workflow_phase") == "testing"
            assert loaded.get("score") == 99.5
        finally:
            store.close()


# =============================================================================
# 5. TEST DAG TASKS VÀ REPOSITORIES TRÊN MYSQL LIVE
# =============================================================================
@pytest.fixture(scope="module")
def db_session_factory():
    """Fixture cung cấp session factory kết nối MySQL."""
    return get_session_factory()


class TestDatabaseRepositoriesLive:
    """Kiểm thử các repositories trên cơ sở dữ liệu MySQL 8.4 live."""

    def test_worker_and_agent_repository(self, db_session_factory) -> None:
        worker_id = f"worker-test-{uuid.uuid4().hex[:6]}"
        agent_id = f"dev-test-{uuid.uuid4().hex[:4]}"

        with db_session_factory() as session:
            # 1. Đăng ký Worker
            worker = WorkerRepository.register_worker(
                session,
                {
                    "worker_id": worker_id,
                    "machine_id": "PC2",
                    "base_url": "http://192.168.1.102:8000",
                    "contract_version": "1.2.0",
                    "worker_version": "1.0.0",
                },
            )
            assert worker.worker_id == worker_id
            session.commit()

            # 2. Đăng ký Agent trên Worker đó
            agent = AgentRepository.upsert_agent(
                session,
                {
                    "agent_id": agent_id,
                    "role": "dev",
                    "machine": "PC2",
                    "runner": "python",
                    "capabilities": ["code_generation"],
                },
            )
            assert agent.agent_id == agent_id
            session.commit()

            # 3. Cập nhật Heartbeat
            worker_updated = WorkerRepository.record_heartbeat(session, worker_id)
            session.commit()

            assert worker_updated is not None
            assert worker_updated.last_heartbeat_at is not None

            # Dọn dẹp
            session.delete(agent)
            session.delete(worker)
            session.commit()

    def test_optimistic_locking_version_conflict(self, db_session_factory) -> None:
        run_id = f"RUN-OPT-{uuid.uuid4().hex[:6]}"
        task_id = f"{run_id}-T1"

        with db_session_factory() as session:
            # Tạo run & task
            WorkflowRunRepository.create_run(
                session,
                {
                    "run_id": run_id,
                    "project_id": "PRJ-OPT",
                    "brief": "Optimistic lock test",
                },
            )
            TaskRepository.create_task(
                session,
                {
                    "task_id": task_id,
                    "run_id": run_id,
                    "agent_id": "pm-01",
                    "role": "pm",
                    "action": "analyze_spec",
                    "instruction": "Test optimistic locking",
                },
            )
            session.commit()

            task = TaskRepository.get_task(session, task_id)
            assert task is not None
            current_version = task.version

            # Cập nhật thành công lần 1 (version 1 -> 2)
            TaskRepository.update_task_state(
                session,
                task_id=task_id,
                expected_version=current_version,
                new_state=DispatchState.ASSIGNED,
            )
            session.commit()

            # Lần cập nhật thứ 2 vẫn dùng version cũ (1) -> phải ném VersionConflictError!
            with pytest.raises(VersionConflictError):
                TaskRepository.update_task_state(
                    session,
                    task_id=task_id,
                    expected_version=current_version,  # Version cũ đã bị lỗi thời!
                    new_state=DispatchState.RUNNING,
                )

            # Dọn dẹp
            run = WorkflowRunRepository.get_run(session, run_id)
            if run:
                session.delete(run)
                session.commit()

    def test_dag_multi_parent_dependencies(self, db_session_factory) -> None:
        """Kiểm tra logic AND của phụ thuộc DAG:
        T1 và T2 độc lập; T3 phụ thuộc cả T1 VÀ T2.
        Chỉ khi cả T1 VÀ T2 đều COMPLETED thì T3 mới READY.
        """
        run_id = f"RUN-DAG-{uuid.uuid4().hex[:6]}"
        t1_id = f"{run_id}-T1"
        t2_id = f"{run_id}-T2"
        t3_id = f"{run_id}-T3"

        with db_session_factory() as session:
            WorkflowRunRepository.create_run(
                session,
                {
                    "run_id": run_id,
                    "project_id": "PRJ-DAG",
                    "brief": "Multi-parent DAG test",
                },
            )
            for tid, agent, role in [(t1_id, "pm-01", "pm"), (t2_id, "ba-01", "ba"), (t3_id, "dev-01", "dev")]:
                TaskRepository.create_task(
                    session,
                    {
                        "task_id": tid,
                        "run_id": run_id,
                        "agent_id": agent,
                        "role": role,
                        "action": "test_action",
                        "instruction": "Instruction",
                    },
                )

            # T3 phụ thuộc cả T1 và T2
            TaskRepository.add_dependency(session, task_id=t3_id, depends_on_task_id=t1_id)
            TaskRepository.add_dependency(session, task_id=t3_id, depends_on_task_id=t2_id)
            session.commit()

            # Ban đầu: T1 và T2 đều READY, T3 CHƯA READY
            ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
            ready_ids = {t.task_id for t in ready_tasks}
            assert ready_ids == {t1_id, t2_id}

            # Hoàn thành T1
            t1 = TaskRepository.get_task(session, t1_id)
            TaskRepository.update_task_state(session, t1_id, DispatchState.ASSIGNED, t1.version)
            t1 = TaskRepository.get_task(session, t1_id)
            TaskRepository.update_task_state(session, t1_id, DispatchState.RUNNING, t1.version)
            t1 = TaskRepository.get_task(session, t1_id)
            TaskRepository.update_task_state(session, t1_id, DispatchState.COMPLETED, t1.version)
            session.commit()

            # Sau khi T1 xong nhưng T2 chưa xong: Chỉ T2 READY, T3 vẫn CHƯA READY!
            ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
            ready_ids = {t.task_id for t in ready_tasks}
            assert ready_ids == {t2_id}

            # Hoàn thành T2
            t2 = TaskRepository.get_task(session, t2_id)
            TaskRepository.update_task_state(session, t2_id, DispatchState.ASSIGNED, t2.version)
            t2 = TaskRepository.get_task(session, t2_id)
            TaskRepository.update_task_state(session, t2_id, DispatchState.RUNNING, t2.version)
            t2 = TaskRepository.get_task(session, t2_id)
            TaskRepository.update_task_state(session, t2_id, DispatchState.COMPLETED, t2.version)
            session.commit()

            # Bây giờ cả T1 và T2 đều xong -> T3 phải READY!
            ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
            ready_ids = {t.task_id for t in ready_tasks}
            assert ready_ids == {t3_id}

            # Dọn dẹp
            run = WorkflowRunRepository.get_run(session, run_id)
            if run:
                session.delete(run)
                session.commit()
