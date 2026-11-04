"""sync.py - Dịch vụ đồng bộ dữ liệu hai chiều ra Linear với hàng đợi Redis, tính idempotent và cách ly lỗi.

Tuân thủ quy tắc Prompt S7 mục 4:
- Mọi thay đổi (project, milestone, task, đổi trạng thái, kết quả, artifact, phê duyệt)
  được đưa vào hàng đợi đồng bộ trong Redis cùng bản ghi bền trong MySQL (linear_links).
- Idempotent: kiểm tra bảng linear_links và content_hash (chạy lại không sinh trùng).
- Rate limit: làm chậm và gộp ghi (LINEAR_MAX_REQ_PER_MIN).
- Xử lý 429: backoff tự động.
- Dead-Letter Queue (DLQ): chuyển vào DLQ sau K lần lỗi và ghi audit log.
- Lỗi Linear KHÔNG BAO GIỜ ném lên orchestrator hay làm hỏng workflow.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import redis
from sqlalchemy.orm import Session

from control.app.database.models import utc_now
from control.app.database.repositories import AuditRepository, LinearLinkRepository
from control.app.database.session import get_session_factory
from control.app.integrations.linear.client import (
    LinearClientError,
    LinearRateLimitError,
    WorkTracker,
)
from control.app.integrations.linear.config_manager import LinearSchemaCache
from control.app.integrations.linear.templates import (
    render_gate_issue_description,
    render_issue_description,
    render_issue_title,
    render_result_comment,
)

logger = logging.getLogger(__name__)

REDIS_SYNC_QUEUE = "linear:sync:queue"
REDIS_SYNC_DLQ = "linear:sync:dlq"


def compute_content_hash(data: Dict[str, Any]) -> str:
    """Tính toán mã băm SHA-256 từ nội dung payload để kiểm tra tính idempotent."""
    raw = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class LinearSyncService:
    """Dịch vụ xử lý đồng bộ thực thể nội bộ lên Linear."""

    def __init__(
        self,
        tracker: WorkTracker,
        schema_cache: LinearSchemaCache,
        redis_client: Optional[redis.Redis] = None,
        session_factory=None,
    ) -> None:
        self.tracker = tracker
        self.schema_cache = schema_cache
        self.redis = redis_client
        self.session_factory = session_factory or get_session_factory()
        self._last_request_time: float = 0.0

    def _throttle(self) -> None:
        """Đảm bảo tốc độ gọi Linear không vượt quá max_req_per_min."""
        max_rpm = max(self.schema_cache.max_req_per_min, 1)
        min_interval = 60.0 / max_rpm
        elapsed = time.time() - self._last_request_time
        if elapsed < min_interval:
            sleep_time = min_interval - elapsed
            time.sleep(sleep_time)
        self._last_request_time = time.time()

    def enqueue_change(
        self,
        entity_type: str,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
    ) -> bool:
        """Đẩy một sự kiện thay đổi vào hàng đợi Redis để đồng bộ nền.

        Nếu Redis gặp sự cố, log cảnh báo và không ném lỗi ra ngoài.
        """
        payload = {
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "entity_type": entity_type,
            "local_id": local_id,
            "run_id": run_id,
            "data": data,
            "attempt": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            if self.redis is not None:
                self.redis.lpush(REDIS_SYNC_QUEUE, json.dumps(payload))
                logger.debug("Đã enqueue sự kiện Linear: %s %s (run=%s)", entity_type, local_id, run_id)
                return True
        except Exception as exc:
            logger.warning("Không thể đẩy sự kiện Linear vào Redis (%s). Bỏ qua để không chặn workflow.", exc)
        return False

    def sync_entity_now(
        self,
        session: Session,
        entity_type: str,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
    ) -> Optional[str]:
        """Đồng bộ trực tiếp một thực thể lên Linear (hỗ trợ gọi đồng bộ hoặc từ worker nền).

        Idempotent: Nếu content_hash không đổi so với lần đồng bộ trước, bỏ qua và trả về linear_id cũ.
        Không bao giờ ném ngoại lệ lên tầng điều phối.
        """
        try:
            content_hash = compute_content_hash(data)
            existing_link = LinearLinkRepository.get_link(session, entity_type, local_id)

            # 1. Kiểm tra Idempotency: nếu hash khớp hoàn toàn, không gọi lại Linear
            if existing_link and existing_link.content_hash == content_hash:
                logger.debug(
                    "Idempotent: %s '%s' có hash không đổi (%s). Bỏ qua gọi Linear.",
                    entity_type, local_id, content_hash[:8],
                )
                return existing_link.linear_id

            self._throttle()

            linear_id = None
            if entity_type == "PROJECT":
                linear_id = self._sync_project(session, local_id, run_id, data, existing_link)
            elif entity_type == "MILESTONE":
                linear_id = self._sync_milestone(session, local_id, run_id, data, existing_link)
            elif entity_type == "TASK":
                linear_id = self._sync_task(session, local_id, run_id, data, existing_link)
            elif entity_type == "RESULT":
                linear_id = self._sync_result(session, local_id, run_id, data, existing_link)
            elif entity_type == "STATUS":
                linear_id = self._sync_status(session, local_id, run_id, data, existing_link)
            elif entity_type == "APPROVAL":
                linear_id = self._sync_approval(session, local_id, run_id, data, existing_link)
            elif entity_type == "ARTIFACT":
                linear_id = self._sync_artifact(session, local_id, run_id, data, existing_link)
            elif entity_type == "RELATION":
                linear_id = self._sync_relation(session, local_id, run_id, data)
            else:
                logger.warning("Không nhận diện được entity_type '%s' khi đồng bộ Linear", entity_type)
                return None

            if linear_id:
                LinearLinkRepository.upsert_link(
                    session=session,
                    entity_type=entity_type,
                    local_id=local_id,
                    linear_id=linear_id,
                    content_hash=content_hash,
                )
                session.commit()
                logger.info("Đã đồng bộ Linear thành công: %s %s -> %s", entity_type, local_id, linear_id)
            return linear_id

        except LinearRateLimitError as exc:
            logger.warning("Chạm trần Linear Rate Limit khi đồng bộ %s %s: %s", entity_type, local_id, exc)
            return None
        except Exception as exc:
            logger.error("Lỗi đồng bộ Linear (%s %s): %s. Không chặn workflow.", entity_type, local_id, exc)
            return None

    def _sync_project(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        name = data.get("name") or f"Run {run_id}"
        description = data.get("brief") or data.get("description", "")
        team_ids = [self.schema_cache.team_id]
        return self.tracker.ensure_project(name=name, description=description, team_ids=team_ids)

    def _sync_milestone(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        # Tìm linear_id của project thuộc run này
        prj_link = LinearLinkRepository.get_link(session, "PROJECT", run_id)
        if not prj_link:
            logger.warning("Chưa có Linear Project cho run '%s', không thể tạo Milestone '%s'", run_id, local_id)
            return None
        name = data.get("name", local_id)
        description = data.get("description")
        return self.tracker.ensure_milestone(name=name, project_id=prj_link.linear_id, description=description)

    def _sync_task(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        # Kiểm tra giới hạn MAX_LINEAR_ISSUES_PER_RUN trước khi tạo mới
        if not existing_link:
            issue_count = LinearLinkRepository.count_issues_by_prefix(session, local_id_prefix=run_id)
            if issue_count >= self.schema_cache.max_issues_per_run:
                logger.warning(
                    "Đã đạt giới hạn MAX_LINEAR_ISSUES_PER_RUN=%d cho run '%s'. "
                    "Tạm dừng đồng bộ task mới '%s' lên Linear để tiết kiệm quota.",
                    self.schema_cache.max_issues_per_run, run_id, local_id,
                )
                return None

        # Project ID trên Linear
        prj_link = LinearLinkRepository.get_link(session, "PROJECT", run_id)
        project_id = prj_link.linear_id if prj_link else None

        # Parent Issue nếu là sub-task
        parent_id = None
        parent_task_id = data.get("parent_task_id")
        if parent_task_id:
            parent_link = LinearLinkRepository.get_link(session, "TASK", parent_task_id)
            if parent_link:
                parent_id = parent_link.linear_id

        # Trạng thái State ID
        dispatch_state = data.get("dispatch_state", "QUEUED")
        state_id = self.schema_cache.dispatch_to_state_id.get(dispatch_state)

        # Nhãn Labels: nhãn role của agent + nhãn blocked nếu đang bị chặn
        label_ids: List[str] = []
        role = data.get("role", "")
        if role in self.schema_cache.role_to_label_id:
            label_ids.append(self.schema_cache.role_to_label_id[role])

        if dispatch_state == "BLOCKED" and "blocked" in self.schema_cache.special_label_ids:
            label_ids.append(self.schema_cache.special_label_ids["blocked"])

        title = render_issue_title(data)
        machine = self.schema_cache.agent_machines.get(data.get("agent_id", ""))
        desc = render_issue_description(
            task=data,
            machine=machine,
            dependencies=data.get("dependencies", []),
            pr_link=data.get("pr_link"),
            artifact_links=data.get("artifact_links", []),
        )

        existing_linear_id = existing_link.linear_id if existing_link else None
        issue_id = self.tracker.upsert_issue(
            title=title,
            description=desc,
            team_id=self.schema_cache.team_id,
            project_id=project_id,
            parent_id=parent_id,
            state_id=state_id,
            label_ids=label_ids,
            issue_id=existing_linear_id,
        )
        return issue_id

    def _sync_status(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        task_link = LinearLinkRepository.get_link(session, "TASK", local_id)
        if not task_link:
            return None
        new_state = data.get("dispatch_state", "QUEUED")
        state_id = self.schema_cache.dispatch_to_state_id.get(new_state)
        if state_id:
            self.tracker.set_status(task_link.linear_id, state_id)

        # Cập nhật nhãn blocked nếu cần
        if new_state == "BLOCKED" and "blocked" in self.schema_cache.special_label_ids:
            blocked_lbl = self.schema_cache.special_label_ids["blocked"]
            role_lbl = self.schema_cache.role_to_label_id.get(data.get("role", ""))
            lbls = [blocked_lbl] + ([role_lbl] if role_lbl else [])
            self.tracker.set_labels(task_link.linear_id, lbls)
        return task_link.linear_id

    def _sync_result(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        task_link = LinearLinkRepository.get_link(session, "TASK", local_id)
        if not task_link:
            return None

        # 1. Cập nhật trạng thái sang Done hoặc Canceled
        status_val = data.get("status", "COMPLETED")
        disp_state = "COMPLETED" if status_val == "COMPLETED" else ("BLOCKED" if status_val == "BLOCKED" else "FAILED")
        state_id = self.schema_cache.dispatch_to_state_id.get(disp_state)
        if state_id:
            self.tracker.set_status(task_link.linear_id, state_id)

        # 2. Thêm Comment kết quả định dạng chuẩn
        comment_text = render_result_comment(
            task=data,
            result_status=status_val,
            attempt=data.get("attempt", 1),
            duration_sec=data.get("duration_sec"),
            link=data.get("link"),
            summary=data.get("summary"),
        )
        self.tracker.add_comment(task_link.linear_id, comment_text)

        # 3. Đính kèm liên kết ngoài nếu có PR
        pr_link = data.get("pr_link") or data.get("link")
        if pr_link and pr_link.startswith("http"):
            self.tracker.attach_link(task_link.linear_id, url=pr_link, title="GitHub Pull Request")

        return task_link.linear_id

    def _sync_approval(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        gate = data.get("gate", "approve_scope")
        gate_title = f"[APPROVAL GATE] Phê duyệt cổng an toàn: {gate} ({run_id})"
        desc = render_gate_issue_description(
            run_id=run_id,
            gate=gate,
            project_id=data.get("project_id", run_id),
            brief=data.get("brief", ""),
        )

        prj_link = LinearLinkRepository.get_link(session, "PROJECT", run_id)
        project_id = prj_link.linear_id if prj_link else None

        state_id = self.schema_cache.dispatch_to_state_id.get("RUNNING")
        label_ids = []
        if "needs_approval" in self.schema_cache.special_label_ids:
            label_ids.append(self.schema_cache.special_label_ids["needs_approval"])

        existing_id = existing_link.linear_id if existing_link else None
        issue_id = self.tracker.upsert_issue(
            title=gate_title,
            description=desc,
            team_id=self.schema_cache.team_id,
            project_id=project_id,
            state_id=state_id,
            label_ids=label_ids,
            issue_id=existing_id,
        )
        return issue_id

    def _sync_artifact(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
        existing_link,
    ) -> Optional[str]:
        task_id = data.get("task_id")
        if not task_id:
            return None
        task_link = LinearLinkRepository.get_link(session, "TASK", task_id)
        if not task_link:
            return None

        url = data.get("uri") or f"artifact://{data.get('artifact_id')}"
        title = f"Artifact: {data.get('name', 'file')} ({data.get('type', 'DATA')})"
        return self.tracker.attach_link(task_link.linear_id, url=url, title=title)

    def _sync_relation(
        self,
        session: Session,
        local_id: str,
        run_id: str,
        data: Dict[str, Any],
    ) -> Optional[str]:
        issue_a = data.get("issue_id")
        issue_b = data.get("related_issue_id")
        link_a = LinearLinkRepository.get_link(session, "TASK", issue_a) if issue_a else None
        link_b = LinearLinkRepository.get_link(session, "TASK", issue_b) if issue_b else None
        if link_a and link_b:
            return self.tracker.add_relation(
                issue_id=link_a.linear_id,
                related_issue_id=link_b.linear_id,
                relation_type=data.get("relation_type", "blocks"),
            )
        return None

    def process_queue_worker(self, max_items: int = 10) -> int:
        """Worker tiến trình nền lấy sự kiện từ Redis và xử lý đồng bộ.

        - Idempotent: qua bảng linear_links và content_hash.
        - Xử lý 429: backoff.
        - DLQ: nếu quá max_sync_attempts thì chuyển vào linear:sync:dlq và ghi audit log.
        - Trả về số lượng item đã xử lý thành công.
        """
        if not self.redis:
            return 0

        processed = 0
        for _ in range(max_items):
            raw = self.redis.rpop(REDIS_SYNC_QUEUE)
            if not raw:
                break

            try:
                item = json.loads(raw)
            except Exception:
                continue

            event_id = item.get("event_id", "")
            entity_type = item.get("entity_type", "")
            local_id = item.get("local_id", "")
            run_id = item.get("run_id", "")
            data = item.get("data", {})
            attempt = item.get("attempt", 0) + 1

            success = False
            with self.session_factory() as session:
                try:
                    res = self.sync_entity_now(session, entity_type, local_id, run_id, data)
                    if res is not None:
                        success = True
                        processed += 1
                except Exception as exc:
                    logger.warning("Thất bại khi xử lý sự kiện Linear %s (lần %d): %s", event_id, attempt, exc)

                if not success:
                    # Kiểm tra chuyển vào Dead-Letter Queue
                    if attempt >= self.schema_cache.max_sync_attempts:
                        logger.error(
                            "Sự kiện Linear %s (%s %s) đã vượt quá %d lần thử. Chuyển vào Dead-Letter Queue.",
                            event_id, entity_type, local_id, self.schema_cache.max_sync_attempts,
                        )
                        self.redis.lpush(REDIS_SYNC_DLQ, json.dumps(item))
                        # Ghi nhận Audit Log
                        AuditRepository.record(
                            session=session,
                            action="LINEAR_SYNC_DEAD_LETTER",
                            result="FAILED",
                            target=f"linear:{entity_type}:{local_id}",
                            run_id=run_id,
                            details={"event_id": event_id, "attempts": attempt, "data": data},
                        )
                        session.commit()
                    else:
                        # Đẩy lại vào hàng đợi để thử lại sau
                        item["attempt"] = attempt
                        self.redis.lpush(REDIS_SYNC_QUEUE, json.dumps(item))

        return processed
