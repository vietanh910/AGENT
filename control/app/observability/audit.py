"""control/app/observability/audit.py - Dịch vụ Audit Log với Hash Chain và xác thực tính toàn vẹn (Prompt S11).

Quy tắc:
- Bảng audit_logs chỉ ghi thêm (append-only).
- Ghi nhận: mọi giao việc, kết quả, phê duyệt, thay đổi trạng thái run, mọi cuộc gọi ra Linear/GitHub, mọi hành động điều khiển ADMIN.
- Hash nối chuỗi (Merkle/Hash-Chain): Mỗi bản ghi chứa prev_hash và entry_hash tính từ bản ghi trước đó.
- Phát hiện bất kỳ hành vi sửa đổi, chèn hoặc xóa dữ liệu kiểm toán trái phép.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session

from control.app.database.models import AuditLogModel, utc_now
from control.app.database.repositories import AuditRepository

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64


class AuditService:
    """Dịch vụ quản trị và kiểm định nhật ký kiểm toán (Audit Log) bất biến."""

    @staticmethod
    def calculate_hash(
        prev_hash: str,
        actor: Optional[str],
        action: str,
        target: Optional[str],
        result: str,
        timestamp: datetime,
        details: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Tính toán entry_hash sử dụng SHA-256 từ chuỗi các trường định danh."""
        details_str = json.dumps(details or {}, sort_keys=True, default=str)
        if timestamp:
            if hasattr(timestamp, "tzinfo") and timestamp.tzinfo is None:
                ts_utc = timestamp.replace(tzinfo=timezone.utc)
            else:
                ts_utc = timestamp
            ts_str = ts_utc.isoformat()
        else:
            ts_str = ""
        raw_payload = f"{prev_hash}|{actor or ''}|{action}|{target or ''}|{result}|{ts_str}|{details_str}"
        return hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()

    @classmethod
    def record(
        cls,
        session: Session,
        action: str,
        result: str,
        actor: Optional[str] = None,
        target: Optional[str] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> AuditLogModel:
        """Ghi thêm một bản ghi Audit mới có nối chuỗi mã băm (Hash-Chained)."""
        # 1. Tìm entry_hash của bản ghi trước đó
        stmt = select(AuditLogModel.entry_hash).order_by(AuditLogModel.id.desc()).limit(1)
        last_hash = session.execute(stmt).scalar()
        prev_hash = last_hash or GENESIS_HASH

        now = utc_now()
        entry_hash = cls.calculate_hash(
            prev_hash=prev_hash,
            actor=actor,
            action=action,
            target=target,
            result=result,
            timestamp=now,
            details=details,
        )

        entry = AuditLogModel(
            run_id=run_id,
            task_id=task_id,
            agent_id=agent_id,
            actor=actor,
            action=action,
            target=target,
            result=result,
            details=details or {},
            timestamp=now,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        session.add(entry)
        session.flush()
        return entry

    @classmethod
    def verify_chain_integrity(cls, session: Session) -> Tuple[bool, Optional[int], Optional[str]]:
        """Kiểm tra tính toàn vẹn của chuỗi hash trong audit_logs.

        Trả về:
        - (True, None, None) nếu chuỗi hoàn toàn hợp lệ, không bị sửa đổi.
        - (False, failed_id, reason) nếu phát hiện bất kỳ bản ghi nào bị can thiệp.
        """
        stmt = select(AuditLogModel).order_by(AuditLogModel.id.asc())
        records = session.execute(stmt).scalars().all()
        if not records:
            return True, None, None

        # Chỉ kiểm tra các bản ghi có tham gia chuỗi hash (bỏ qua bản ghi legacy chưa có entry_hash)
        chained_records = [r for r in records if r.entry_hash is not None]
        if not chained_records:
            return True, None, None

        expected_prev = chained_records[0].prev_hash or GENESIS_HASH
        for rec in chained_records:
            # Kiểm tra prev_hash có khớp với bản ghi trước không
            if rec.prev_hash != expected_prev:
                return False, rec.id, f"Mã prev_hash tại bản ghi id={rec.id} sai lệch: kỳ vọng '{expected_prev}', nhận được '{rec.prev_hash}'"

            # Tính lại entry_hash từ nội dung bản ghi
            recomputed = cls.calculate_hash(
                prev_hash=rec.prev_hash,
                actor=rec.actor,
                action=rec.action,
                target=rec.target,
                result=rec.result,
                timestamp=rec.timestamp,
                details=rec.details,
            )
            if rec.entry_hash != recomputed:
                return False, rec.id, f"Nội dung bản ghi id={rec.id} đã bị can thiệp (hash thực tế: {rec.entry_hash}, hash tính lại: {recomputed})"

            expected_prev = rec.entry_hash

        return True, None, None

    # --- Các phương thức chuyên dụng theo yêu cầu Prompt S11 ---

    @classmethod
    def log_task_dispatched(
        cls, session: Session, run_id: str, task_id: str, worker_id: str, agent_id: str, attempt: int
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="TASK_DISPATCHED",
            result="ASSIGNED",
            actor="DISPATCHER",
            target=task_id,
            run_id=run_id,
            task_id=task_id,
            agent_id=agent_id,
            details={"worker_id": worker_id, "attempt": attempt},
        )

    @classmethod
    def log_task_result(
        cls, session: Session, run_id: str, task_id: str, agent_id: str, status: str, worker_id: str
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="TASK_RESULT",
            result=status,
            actor=worker_id,
            target=task_id,
            run_id=run_id,
            task_id=task_id,
            agent_id=agent_id,
            details={"worker_id": worker_id, "status": status},
        )

    @classmethod
    def log_approval(
        cls, session: Session, run_id: str, gate: str, status: str, actor: str = "ADMIN"
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="APPROVAL",
            result=status,
            actor=actor,
            target=gate,
            run_id=run_id,
            details={"gate": gate, "status": status},
        )

    @classmethod
    def log_run_status(
        cls, session: Session, run_id: str, old_status: str, new_status: str, reason: str = ""
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="RUN_STATUS_CHANGED",
            result=new_status,
            actor="ORCHESTRATOR",
            target=run_id,
            run_id=run_id,
            details={"old_status": old_status, "new_status": new_status, "reason": reason},
        )

    @classmethod
    def log_linear_call(
        cls, session: Session, action_name: str, success: bool, details: Optional[Dict[str, Any]] = None
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="LINEAR_CALL",
            result="SUCCESS" if success else "FAILED",
            actor="LINEAR_ADAPTER",
            target=action_name,
            details=details or {},
        )

    @classmethod
    def log_github_call(
        cls, session: Session, endpoint: str, success: bool, details: Optional[Dict[str, Any]] = None
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="GITHUB_CALL",
            result="SUCCESS" if success else "FAILED",
            actor="GITHUB_ADAPTER",
            target=endpoint,
            details=details or {},
        )

    @classmethod
    def log_admin_action(
        cls, session: Session, action: str, target: str, actor: str = "ADMIN", details: Optional[Dict[str, Any]] = None
    ) -> AuditLogModel:
        return cls.record(
            session=session,
            action="ADMIN_ACTION",
            result="EXECUTED",
            actor=actor,
            target=target,
            details=details or {},
        )
