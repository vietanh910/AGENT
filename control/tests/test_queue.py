"""Kiểm thử tầng hàng đợi Redis, khóa phân tán và phát sự kiện (S3).

Sử dụng fakeredis để đảm bảo test chạy offline độc lập hoàn toàn.
"""

from __future__ import annotations

import time
import pytest
import fakeredis

from agent_contract.system_models import SystemEventType
from control.app.queue.redis import (
    RedisQueueBackend,
    RedisLockBackend,
    DistributedLock,
    IdempotencyManager,
    task_lock,
)
from control.app.queue.events import (
    EventPublisher,
    EventSubscriber,
)


@pytest.fixture
def fake_redis_client():
    """Fixture cung cấp client FakeRedis độc lập."""
    return fakeredis.FakeRedis(decode_responses=True)


class TestRedisQueue:
    """Kiểm thử hàng đợi per-agent."""

    def test_queue_push_pop_fifo(self, fake_redis_client) -> None:
        queue = RedisQueueBackend(fake_redis_client)
        agent_id = "test-agent-dev"

        assert queue.get_queue_length(agent_id) == 0
        queue.push_task(agent_id, "T-1")
        queue.push_task(agent_id, "T-2")
        queue.push_task(agent_id, "T-3")

        assert queue.get_queue_length(agent_id) == 3

        # LIFO / FIFO: RPUSH và LPOP -> FIFO
        assert queue.pop_task(agent_id) == "T-1"
        assert queue.pop_task(agent_id) == "T-2"
        assert queue.pop_task(agent_id) == "T-3"
        assert queue.pop_task(agent_id) is None
        assert queue.get_queue_length(agent_id) == 0

    def test_queue_clear(self, fake_redis_client) -> None:
        queue = RedisQueueBackend(fake_redis_client)
        agent_id = "test-agent-pm"

        queue.push_task(agent_id, "T-10")
        queue.clear_queue(agent_id)
        assert queue.get_queue_length(agent_id) == 0


class TestRedisDistributedLock:
    """Kiểm thử khóa phân tán lock:task:{id}."""

    def test_acquire_and_release(self, fake_redis_client) -> None:
        backend = RedisLockBackend(fake_redis_client)
        lock_key = "lock:task:TASK-123"

        token = backend.acquire(lock_key, ttl_seconds=60)
        assert token is not None

        # Tiến trình thứ 2 cùng chiếm khóa -> thất bại
        second_token = backend.acquire(lock_key, ttl_seconds=60)
        assert second_token is None

        # Giải phóng sai token -> thất bại
        assert backend.release(lock_key, "wrong-token") is False

        # Giải phóng đúng token -> thành công
        assert backend.release(lock_key, token) is True

        # Sau khi giải phóng, chiếm lại thành công
        third_token = backend.acquire(lock_key, ttl_seconds=60)
        assert third_token is not None
        backend.release(lock_key, third_token)

    def test_renew_lock(self, fake_redis_client) -> None:
        backend = RedisLockBackend(fake_redis_client)
        lock_key = "lock:task:TASK-456"

        token = backend.acquire(lock_key, ttl_seconds=10)
        assert token is not None

        # Gia hạn sai token -> False
        assert backend.renew(lock_key, "wrong-token", extension_seconds=20) is False

        # Gia hạn đúng token -> True
        assert backend.renew(lock_key, token, extension_seconds=300) is True

        # TTL trên Redis phải được cập nhật
        ttl = fake_redis_client.ttl(lock_key)
        assert ttl > 10

        backend.release(lock_key, token)

    def test_distributed_lock_context_manager(self, fake_redis_client) -> None:
        backend = RedisLockBackend(fake_redis_client)
        lock_key = "lock:task:TASK-CTX"

        lock = DistributedLock(backend, lock_key, ttl_seconds=60)
        with lock:
            assert lock.token is not None
            # Trong block, không ai chiếm được
            assert backend.acquire(lock_key, ttl_seconds=60) is None

        # Ra khỏi block, khóa đã được tự động giải phóng
        assert fake_redis_client.get(lock_key) is None

    def test_task_lock_helper(self, fake_redis_client) -> None:
        task_id = "TASK-AUTO-01"
        with task_lock(task_id, fake_redis_client, ttl_seconds=300, auto_renew=False) as lock:
            assert lock.token is not None
            assert fake_redis_client.exists(f"lock:task:{task_id}")


class TestIdempotencyManager:
    """Kiểm thử khóa chống trùng lặp Webhook."""

    def test_check_and_claim(self, fake_redis_client) -> None:
        mgr = IdempotencyManager(fake_redis_client, default_ttl_seconds=60)
        webhook_id = "github-event-delivery-789"

        # Lần 1: Nhận xử lý
        assert mgr.check_and_claim(webhook_id) is True
        assert mgr.is_claimed(webhook_id) is True

        # Lần 2 (trùng lặp): Bị từ chối
        assert mgr.check_and_claim(webhook_id) is False

        # Giải phóng
        mgr.release(webhook_id)
        assert mgr.is_claimed(webhook_id) is False

        # Sau khi giải phóng, nhận lại được
        assert mgr.check_and_claim(webhook_id) is True


class TestEventPublishingAndStreaming:
    """Kiểm thử phát và đọc luồng sự kiện Redis Stream."""

    def test_publish_and_read_events(self, fake_redis_client) -> None:
        publisher = EventPublisher(fake_redis_client)
        subscriber = EventSubscriber(fake_redis_client)
        run_id = "RUN-EV-01"

        msg_id1 = publisher.publish(
            event_type=SystemEventType.RUN_STARTED,
            payload={"run_id": run_id, "initiator": "user"},
            run_id=run_id,
        )
        msg_id2 = publisher.publish(
            event_type=SystemEventType.TASK_STARTED,
            payload={"task_id": "T-1"},
            run_id=run_id,
            task_id="T-1",
        )
        assert msg_id1 is not None
        assert msg_id2 is not None

        # Đọc sự kiện của run_id
        events = subscriber.read_events(run_id=run_id, count=10)
        assert len(events) == 2
        assert events[0]["event_type"] == SystemEventType.RUN_STARTED.value
        assert events[0]["payload"]["run_id"] == run_id
        assert events[1]["event_type"] == SystemEventType.TASK_STARTED.value
        assert events[1]["task_id"] == "T-1"

    def test_stream_events_generator(self, fake_redis_client) -> None:
        publisher = EventPublisher(fake_redis_client)
        subscriber = EventSubscriber(fake_redis_client)
        run_id = "RUN-EV-STREAM-01"

        publisher.publish(
            event_type=SystemEventType.RUN_COMPLETED,
            payload={"verdict": "SUCCESS"},
            run_id=run_id,
        )

        stream_gen = subscriber.stream_events(run_id=run_id, last_id="0-0", block_ms=500, max_events=1)
        events = list(stream_gen)
        assert len(events) == 1
        assert events[0]["event_type"] == SystemEventType.RUN_COMPLETED.value
