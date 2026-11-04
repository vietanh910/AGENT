"""Kịch bản kiểm tra khói (Smoke Test) cho tầng lưu trữ MySQL và Redis của Control Node.

Kiểm tra:
1. Xác thực phiên bản MySQL Server (>= 8.0.19 và < 9.6.0).
2. LangGraph Checkpoint Store: setup, put snapshot và get snapshot.
3. Luồng tính toán DAG READY tasks:
   - Tạo run RUN-SMOKE-01 và 3 tasks: T1 -> T2 -> T3.
   - Xác nhận ban đầu chỉ có T1 ở trạng thái READY.
   - Cập nhật T1 hoàn thành -> xác nhận T2 chuyển sang READY.
   - Cập nhật T2 hoàn thành -> xác nhận T3 chuyển sang READY.
   - Cập nhật T3 hoàn thành -> xác nhận không còn task nào READY.
4. Hàng đợi Redis và cơ chế phát hiện sự kiện.
5. Dọn dẹp dữ liệu thử nghiệm.
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
from datetime import datetime, timezone

# Đảm bảo đường dẫn gốc của repository có trong sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Đảm bảo UTF-8 cho console Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent_contract.system_models import RunStatus, DispatchState, SystemEventType
from control.app.database.session import (
    get_session_factory,
    get_engine,
    verify_mysql_server_version,
)
from control.app.database.repositories import (
    WorkflowRunRepository,
    TaskRepository,
)
from control.app.database.checkpoint import MySQLCheckpointStore
from control.app.queue.redis import (
    get_redis_client,
    RedisQueueBackend,
    task_lock,
    IdempotencyManager,
)
from control.app.queue.events import EventPublisher, EventSubscriber


def run_smoke_test() -> bool:
    print("=" * 70)
    print("🚀 BẮT ĐẦU SMOKE TEST: TẦNG LƯU TRỮ VÀ HÀNG ĐỢI CONTROL NODE PC1")
    print("=" * 70)

    engine = get_engine()
    session_factory = get_session_factory()

    # 1. Kiểm tra phiên bản MySQL
    print("\n[1/5] Kiểm tra phiên bản MySQL Server...")
    with engine.connect() as conn:
        ver_tuple = verify_mysql_server_version(conn)
        print(f"  ✅ Kết nối MySQL thành công: Phiên bản {ver_tuple}")

    # 2. Kiểm tra LangGraph Checkpoint Store
    print("\n[2/5] Kiểm tra LangGraph Checkpoint Store...")
    thread_id = "SMOKE-THREAD-CHECKPOINT-01"
    cp_store = MySQLCheckpointStore()
    try:
        cp_store.setup()
        sample_state = {
            "current_step": "analyze_requirements",
            "findings": ["Finding 1", "Finding 2"],
            "step_count": 42,
        }
        cp_id = cp_store.put_state(thread_id, sample_state, metadata={"author": "smoke_test"})
        print(f"  ✅ Đã lưu checkpoint snapshot (ID: {cp_id})")

        loaded_state = cp_store.get_state(thread_id)
        assert loaded_state is not None, "Không đọc được state từ CheckpointStore!"
        assert loaded_state.get("step_count") == 42, f"Dữ liệu state sai lệch: {loaded_state}"
        print(f"  ✅ Đã đọc lại checkpoint thành công: step_count={loaded_state.get('step_count')}")
    finally:
        cp_store.close()

    # 3. Kiểm tra DAG READY Tasks và Luồng Chuyển Trạng Thái
    print("\n[3/5] Kiểm tra DAG Tasks và READY Computation trong MySQL...")
    run_id = "RUN-SMOKE-DAG-01"
    t1_id = f"{run_id}-T1"
    t2_id = f"{run_id}-T2"
    t3_id = f"{run_id}-T3"

    with session_factory() as session:
        # Xóa dữ liệu cũ nếu còn sót lại từ lần chạy trước
        existing_run = WorkflowRunRepository.get_run(session, run_id)
        if existing_run:
            session.delete(existing_run)
            session.commit()

        # Tạo Workflow Run
        run = WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PROJECT-SMOKE-01",
                "brief": "Smoke test workflow run for storage verification",
                "status": RunStatus.RUNNING.value,
                "plan": {"tasks": [t1_id, t2_id, t3_id]},
            },
        )
        session.commit()
        print(f"  ✅ Đã tạo WorkflowRun: {run.run_id} (status: {run.status})")

        # Tạo 3 task: T1 (không phụ thuộc), T2 (phụ thuộc T1), T3 (phụ thuộc T2)
        TaskRepository.create_task(
            session,
            {
                "task_id": t1_id,
                "run_id": run_id,
                "agent_id": "pm-01",
                "role": "pm",
                "action": "analyze_spec",
                "instruction": "Analyze requirement specification",
                "dispatch_state": DispatchState.QUEUED.value,
            },
        )
        TaskRepository.create_task(
            session,
            {
                "task_id": t2_id,
                "run_id": run_id,
                "agent_id": "dev-01",
                "role": "dev",
                "action": "implement_feature",
                "instruction": "Implement backend feature",
                "dispatch_state": DispatchState.QUEUED.value,
            },
        )
        TaskRepository.create_task(
            session,
            {
                "task_id": t3_id,
                "run_id": run_id,
                "agent_id": "tester-01",
                "role": "tester",
                "action": "execute_test_suite",
                "instruction": "Execute test suite and report verdict",
                "dispatch_state": DispatchState.QUEUED.value,
            },
        )

        # Thêm phụ thuộc DAG
        TaskRepository.add_dependency(session, task_id=t2_id, depends_on_task_id=t1_id)
        TaskRepository.add_dependency(session, task_id=t3_id, depends_on_task_id=t2_id)
        session.commit()

        # Kiểm tra ban đầu: Chỉ T1 READY
        ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
        ready_ids = [t.task_id for t in ready_tasks]
        print(f"  👉 Giai đoạn ban đầu - READY tasks: {ready_ids}")
        assert ready_ids == [t1_id], f"Kỳ vọng [{t1_id}], nhưng nhận được {ready_ids}"

        # Mô phỏng Worker claim T1 và hoàn thành T1
        claimed_t1 = TaskRepository.claim_next_ready_task(session, agent_id="pm-01", run_id=run_id)
        assert claimed_t1 is not None and claimed_t1.task_id == t1_id
        TaskRepository.update_task_state(
            session,
            task_id=t1_id,
            expected_version=claimed_t1.version,
            new_state=DispatchState.RUNNING,
        )
        t1_running = TaskRepository.get_task(session, t1_id)
        assert t1_running is not None
        TaskRepository.update_task_state(
            session,
            task_id=t1_id,
            expected_version=t1_running.version,
            new_state=DispatchState.COMPLETED,
        )
        session.commit()

        # Kiểm tra sau khi T1 hoàn thành: T2 phải READY
        ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
        ready_ids = [t.task_id for t in ready_tasks]
        print(f"  👉 Sau khi T1 COMPLETED - READY tasks: {ready_ids}")
        assert ready_ids == [t2_id], f"Kỳ vọng [{t2_id}], nhưng nhận được {ready_ids}"

        # Hoàn thành T2 (tuân thủ state transition: QUEUED -> ASSIGNED -> RUNNING -> COMPLETED)
        t2_curr = TaskRepository.get_task(session, t2_id)
        assert t2_curr is not None
        TaskRepository.update_task_state(
            session,
            task_id=t2_id,
            expected_version=t2_curr.version,
            new_state=DispatchState.ASSIGNED,
        )
        t2_assigned = TaskRepository.get_task(session, t2_id)
        assert t2_assigned is not None
        TaskRepository.update_task_state(
            session,
            task_id=t2_id,
            expected_version=t2_assigned.version,
            new_state=DispatchState.RUNNING,
        )
        t2_running = TaskRepository.get_task(session, t2_id)
        assert t2_running is not None
        TaskRepository.update_task_state(
            session,
            task_id=t2_id,
            expected_version=t2_running.version,
            new_state=DispatchState.COMPLETED,
        )
        session.commit()

        # Kiểm tra sau khi T2 hoàn thành: T3 phải READY
        ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
        ready_ids = [t.task_id for t in ready_tasks]
        print(f"  👉 Sau khi T2 COMPLETED - READY tasks: {ready_ids}")
        assert ready_ids == [t3_id], f"Kỳ vọng [{t3_id}], nhưng nhận được {ready_ids}"

        # Hoàn thành T3 (tuân thủ state transition: QUEUED -> ASSIGNED -> RUNNING -> COMPLETED)
        t3_curr = TaskRepository.get_task(session, t3_id)
        assert t3_curr is not None
        TaskRepository.update_task_state(
            session,
            task_id=t3_id,
            expected_version=t3_curr.version,
            new_state=DispatchState.ASSIGNED,
        )
        t3_assigned = TaskRepository.get_task(session, t3_id)
        assert t3_assigned is not None
        TaskRepository.update_task_state(
            session,
            task_id=t3_id,
            expected_version=t3_assigned.version,
            new_state=DispatchState.RUNNING,
        )
        t3_running = TaskRepository.get_task(session, t3_id)
        assert t3_running is not None
        TaskRepository.update_task_state(
            session,
            task_id=t3_id,
            expected_version=t3_running.version,
            new_state=DispatchState.COMPLETED,
        )
        session.commit()

        # Kiểm tra sau khi T3 hoàn thành: không còn task nào READY
        ready_tasks = TaskRepository.get_ready_tasks(session, run_id)
        ready_ids = [t.task_id for t in ready_tasks]
        print(f"  👉 Sau khi T3 COMPLETED - READY tasks: {ready_ids}")
        assert ready_ids == [], f"Kỳ vọng [], nhưng nhận được {ready_ids}"
        print("  ✅ DAG READY computation và chuyển trạng thái đạt 100% chính xác!")

    # 4. Kiểm tra Redis Queue, Khóa phân tán và Events
    print("\n[4/5] Kiểm tra Redis Queue, Distributed Lock và Event Broadcasting...")
    redis_client = get_redis_client()
    queue = RedisQueueBackend(redis_client)
    idempotency = IdempotencyManager(redis_client)
    publisher = EventPublisher(redis_client)
    subscriber = EventSubscriber(redis_client)

    # Test Queue
    test_agent = "smoke-agent-dev"
    queue.clear_queue(test_agent)
    queue.push_task(test_agent, "task-item-1")
    queue.push_task(test_agent, "task-item-2")
    assert queue.get_queue_length(test_agent) == 2
    popped = queue.pop_task(test_agent)
    assert popped == "task-item-1"
    print("  ✅ Redis Queue push/pop hoạt động chính xác")

    # Test Distributed Lock
    with task_lock(task_id="SMOKE-LOCK-01", client=redis_client, ttl_seconds=10, auto_renew=False) as lock:
        assert lock.token is not None
        # Khóa cùng ID phải bị từ chối
        assert task_lock(task_id="SMOKE-LOCK-01", client=redis_client).acquire() is False
    print("  ✅ Distributed Task Lock (TTL 5m) hoạt động chính xác")

    # Test Idempotency
    idempotency_key = "webhook-delivery-12345"
    assert idempotency.check_and_claim(idempotency_key, ttl_seconds=60) is True
    assert idempotency.check_and_claim(idempotency_key, ttl_seconds=60) is False
    idempotency.release(idempotency_key)
    print("  ✅ Idempotency Manager (chống trùng lặp Webhook) hoạt động chính xác")

    # Test Event Broadcasting
    msg_id = publisher.publish(
        event_type=SystemEventType.RUN_STARTED,
        payload={"message": "Smoke test workflow run started"},
        run_id=run_id,
    )
    events = subscriber.read_events(run_id=run_id, count=10)
    assert len(events) >= 1
    assert events[-1]["event_type"] == SystemEventType.RUN_STARTED.value
    print(f"  ✅ Phát và đọc sự kiện Redis Stream thành công (Message ID: {msg_id})")

    # 5. Dọn dẹp dữ liệu thử nghiệm
    print("\n[5/5] Dọn dẹp dữ liệu thử nghiệm...")
    with session_factory() as session:
        run_to_clean = WorkflowRunRepository.get_run(session, run_id)
        if run_to_clean:
            session.delete(run_to_clean)
            session.commit()
    queue.clear_queue(test_agent)
    print("  ✅ Đã dọn dẹp xong dữ liệu thử nghiệm sạch sẽ.")

    print("\n" + "=" * 70)
    print("🎉 TẤT CẢ CÁC BƯỚC SMOKE TEST ĐÃ VƯỢT QUA XUẤT SẮC!")
    print("=" * 70)
    return True


if __name__ == "__main__":
    success = run_smoke_test()
    if not success:
        sys.exit(1)
