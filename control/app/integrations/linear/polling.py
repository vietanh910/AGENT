"""polling.py - Xử lý chiều ngược từ người dùng Linear (Polling thay đổi & Webhook tùy chọn).

Tuân thủ quy tắc Prompt S7 mục 6:
- Polling list_changes_since mỗi LINEAR_POLL_SECONDS (mặc định 20s).
- Nhận phê duyệt:
  + Chuyển issue cổng phê duyệt sang trạng thái Done
  + Hoặc comment chứa lệnh /approve hoặc /reject
- Nhận thông tin bổ sung cho task bị chặn: comment chứa /unblock <thông tin>.
- Chỉ các issue cổng phê duyệt và lệnh trong comment mới có hiệu lực;
  mọi chỉnh sửa tay khác trên issue do agent quản lý chỉ được ghi log (LINEAR_MANUAL_EDIT_IGNORED).
- Webhook tùy chọn có kiểm tra chữ ký HMAC-SHA256 từ header Linear-Signature.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from control.app.database.models import utc_now
from control.app.database.repositories import (
    ApprovalRepository,
    AuditRepository,
    LinearLinkRepository,
    TaskRepository,
)
from control.app.database.session import get_session_factory
from control.app.integrations.linear.client import WorkTracker
from control.app.integrations.linear.config_manager import LinearSchemaCache

logger = logging.getLogger(__name__)


def verify_linear_webhook_signature(
    raw_body: bytes,
    signature_header: Optional[str],
    secret: str,
) -> bool:
    """Xác thực chữ ký HMAC-SHA256 của Linear Webhook từ header Linear-Signature."""
    if not signature_header or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.strip())


class LinearPollingService:
    """Dịch vụ polling định kỳ lắng nghe phản hồi từ người dùng trên Linear."""

    def __init__(
        self,
        tracker: WorkTracker,
        schema_cache: LinearSchemaCache,
        session_factory=None,
        lookback_seconds: int = 300,
    ) -> None:
        self.tracker = tracker
        self.schema_cache = schema_cache
        self.session_factory = session_factory or get_session_factory()
        # Khởi tạo thời điểm bắt đầu theo dõi
        self.last_polled_at = datetime.now(timezone.utc) - timedelta(seconds=lookback_seconds)
        self.processed_comment_ids: set[str] = set()

    def poll_once(self) -> Dict[str, int]:
        """Thực hiện một chu kỳ quét kiểm tra thay đổi từ Linear."""
        now = datetime.now(timezone.utc)
        stats = {
            "changes_detected": 0,
            "approvals_processed": 0,
            "unblocks_processed": 0,
            "manual_edits_logged": 0,
        }

        try:
            changes = self.tracker.list_changes_since(self.last_polled_at)
        except Exception as exc:
            logger.warning("Lỗi khi polling Linear changes: %s", exc)
            return stats

        self.last_polled_at = now
        stats["changes_detected"] = len(changes)

        with self.session_factory() as session:
            for issue_data in changes:
                issue_id = issue_data.get("id", "")
                link = LinearLinkRepository.get_by_linear_id(session, issue_id)
                if not link:
                    continue

                # 1. Xử lý Issue cổng phê duyệt (APPROVAL)
                if link.entity_type == "APPROVAL":
                    processed = self._handle_approval_issue(session, link, issue_data)
                    if processed:
                        stats["approvals_processed"] += 1

                # 2. Xử lý Issue của Task (TASK)
                elif link.entity_type == "TASK":
                    unblocked, edited = self._handle_task_issue(session, link, issue_data)
                    if unblocked:
                        stats["unblocks_processed"] += 1
                    if edited:
                        stats["manual_edits_logged"] += 1

            session.commit()

        return stats

    def _handle_approval_issue(
        self,
        session: Session,
        link,
        issue_data: Dict[str, Any],
    ) -> bool:
        """Xử lý phê duyệt từ Gate Approval Issue."""
        local_id = link.local_id  # Thường có dạng APP-{run_id}-{gate} hoặc {gate}
        run_id = ""
        gate = ""
        if local_id.startswith("APP-"):
            parts = local_id[4:].split("-", 1)
            run_id = parts[0]
            gate = parts[1] if len(parts) > 1 else ""
        else:
            gate = local_id

        # Kiểm tra chuyển trạng thái issue trên Linear UI sang Done hoặc Canceled
        st_obj = issue_data.get("state", {})
        st_name = st_obj.get("name", "").lower()
        st_type = st_obj.get("type", "").lower()

        approved = False
        decision = None
        decided_by = "linear_ui"
        reason = ""

        if st_name == "done" or st_type == "completed":
            decision = "APPROVED"
            reason = "Chuyển trạng thái issue sang Done trên Linear UI"
            approved = True
        elif st_name == "canceled" or st_type == "canceled":
            decision = "REJECTED"
            reason = "Chuyển trạng thái issue sang Canceled trên Linear UI"
            approved = True

        # Kiểm tra các comment mới chứa lệnh /approve hoặc /reject
        comments = issue_data.get("comments", {}).get("nodes", [])
        for c in comments:
            cid = c.get("id", "")
            if cid in self.processed_comment_ids:
                continue
            self.processed_comment_ids.add(cid)

            body = c.get("body", "").strip()
            user_name = c.get("user", {}).get("name", "linear_user")
            if "/approve" in body:
                decision = "APPROVED"
                decided_by = f"linear_comment:{user_name}"
                reason = body
                approved = True
                break
            elif "/reject" in body:
                decision = "REJECTED"
                decided_by = f"linear_comment:{user_name}"
                reason = body
                approved = True
                break

        if approved and decision:
            logger.info("Nhận quyết định phê duyệt từ Linear: %s (run=%s, gate=%s) bởi %s", decision, run_id, gate, decided_by)
            ApprovalRepository.record_decision(
                session=session,
                run_id=run_id,
                gate=gate,
                status=decision,
                decided_by=decided_by,
                comment=reason,
            )
            AuditRepository.record(
                session=session,
                action=f"LINEAR_GATE_{decision}",
                result="SUCCESS",
                target=f"approval:{gate}",
                run_id=run_id,
                details={"decided_by": decided_by, "reason": reason, "linear_issue_id": issue_data.get("id")},
            )
            return True
        return False

    def _handle_task_issue(
        self,
        session: Session,
        link,
        issue_data: Dict[str, Any],
    ) -> tuple[bool, bool]:
        """Xử lý phản hồi trên task issue (bổ sung context cho task bị chặn hoặc ghi log chỉnh sửa tay)."""
        task_id = link.local_id
        task = TaskRepository.get_task(session, task_id)
        if not task:
            return False, False

        unblocked = False
        # 1. Kiểm tra comment có chứa /unblock hoặc bổ sung ngữ cảnh khi task bị BLOCKED
        comments = issue_data.get("comments", {}).get("nodes", [])
        for c in comments:
            cid = c.get("id", "")
            if cid in self.processed_comment_ids:
                continue
            self.processed_comment_ids.add(cid)

            body = c.get("body", "").strip()
            user_name = c.get("user", {}).get("name", "linear_user")

            if "/unblock" in body or (task.dispatch_state == "BLOCKED" and len(body) > 5):
                logger.info("Nhận thông tin gỡ chặn từ người dùng Linear cho task '%s' từ %s", task_id, user_name)
                AuditRepository.record(
                    session=session,
                    action="LINEAR_TASK_UNBLOCKED_BY_USER",
                    result="SUCCESS",
                    task_id=task_id,
                    run_id=task.run_id,
                    details={"unblock_comment": body, "user": user_name},
                )
                unblocked = True

        # 2. Mọi chỉnh sửa tay khác về trạng thái hoặc mô tả trên task issue do agent quản lý CHỈ ĐƯỢC GHI LOG
        # Không bao giờ đảo ngược trạng thái của task trong DB
        st_obj = issue_data.get("state", {})
        expected_state_id = self.schema_cache.dispatch_to_state_id.get(task.dispatch_state)
        actual_state_id = st_obj.get("id")

        manual_edited = False
        if expected_state_id and actual_state_id and expected_state_id != actual_state_id:
            logger.info(
                "Phát hiện chỉnh sửa tay trên Linear task issue '%s': trạng thái Linear '%s' != DB '%s'. Ghi log kiểm toán.",
                task_id, st_obj.get("name"), task.dispatch_state,
            )
            AuditRepository.record(
                session=session,
                action="LINEAR_MANUAL_EDIT_IGNORED",
                result="LOGGED",
                task_id=task_id,
                run_id=task.run_id,
                details={
                    "linear_state": st_obj.get("name"),
                    "db_dispatch_state": task.dispatch_state,
                    "notice": "Hệ thống chỉ chấp nhận lệnh qua Gate Issue hoặc comment /approve, /unblock.",
                },
            )
            manual_edited = True

        return unblocked, manual_edited
