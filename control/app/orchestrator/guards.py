"""guards.py - Các quy tắc bảo vệ và chặn ngưỡng an toàn cho Workflow Run (Prompt S9).

Nguyên tắc:
- MAX_FIX_LOOPS = 3 chung cho cả run.
- MAX_TASKS_PER_RUN = 60 (hoặc đọc từ settings).
- RUN_TIMEOUT_MINUTES = 240 (hoặc đọc từ settings).
- max_attempts cho mỗi task (mặc định 3).
- Vượt ngưỡng thì chuyển run sang BLOCKED, ghi audit log, không bao giờ chạy vô hạn.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple, Union

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from control.app.config import settings
from control.app.database.models import TaskModel, WorkflowRunModel, utc_now
from control.app.database.repositories import AuditRepository

logger = logging.getLogger(__name__)

# Ngưỡng mặc định
DEFAULT_MAX_FIX_LOOPS: int = 3
DEFAULT_MAX_TASKS_PER_RUN: int = 60
DEFAULT_RUN_TIMEOUT_MINUTES: int = 240
DEFAULT_MAX_ATTEMPTS: int = 3
DEFAULT_MAX_RUN_RETRIES: int = 15


class GuardViolationError(Exception):
    """Ngoại lệ khi vi phạm các quy tắc bảo vệ an toàn."""


class WorkflowGuards:
    """Tập hợp các bộ kiểm tra điều kiện bảo vệ (Guards) cho Orchestrator."""

    def __init__(
        self,
        max_fix_loops: Optional[int] = None,
        max_tasks_per_run: Optional[int] = None,
        run_timeout_minutes: Optional[int] = None,
    ) -> None:
        self.max_fix_loops = max_fix_loops if max_fix_loops is not None else getattr(settings, "MAX_FIX_LOOPS", DEFAULT_MAX_FIX_LOOPS)
        self.max_tasks_per_run = max_tasks_per_run if max_tasks_per_run is not None else getattr(settings, "MAX_TASKS_PER_RUN", DEFAULT_MAX_TASKS_PER_RUN)
        self.run_timeout_minutes = run_timeout_minutes if run_timeout_minutes is not None else getattr(settings, "RUN_TIMEOUT_MINUTES", DEFAULT_RUN_TIMEOUT_MINUTES)

    def check_fix_loops(self, fix_loop_count: int) -> Tuple[bool, Optional[str]]:
        """Kiểm tra số vòng lặp sửa lỗi tự động (TESTER FAIL, SECURITY FAIL, Build FAIL).

        Nếu fix_loop_count > max_fix_loops, từ chối lặp tiếp để tránh tốn token vô hạn.
        """
        if fix_loop_count > self.max_fix_loops:
            msg = (
                f"Đã vượt quá giới hạn MAX_FIX_LOOPS số vòng sửa lỗi tự động ({fix_loop_count} > {self.max_fix_loops}). "
                f"Chuyển WorkflowRun sang trạng thái BLOCKED để can thiệp thủ công."
            )
            return False, msg
        return True, None

    def check_task_count(self, session: Session, run_id: str) -> Tuple[bool, Optional[str]]:
        """Kiểm tra tổng số task đã sinh cho một WorkflowRun."""
        stmt = select(func.count(TaskModel.task_id)).where(TaskModel.run_id == run_id)
        current_count = session.scalar(stmt) or 0
        if current_count >= self.max_tasks_per_run:
            msg = (
                f"Tổng số task ({current_count}) đã đạt hoặc vượt giới hạn MAX_TASKS_PER_RUN "
                f"({self.max_tasks_per_run} tasks). Ngăn chặn sinh thêm task mới."
            )
            return False, msg
        return True, None

    def check_run_timeout(
        self,
        run_or_session: Union[WorkflowRunModel, Session],
        run_id: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Kiểm tra thời gian chạy tối đa của toàn bộ WorkflowRun từ lúc tạo."""
        if isinstance(run_or_session, Session):
            run = run_or_session.get(WorkflowRunModel, run_id)
        else:
            run = run_or_session

        if not run or not run.created_at:
            return True, None

        now = utc_now()
        created_at = run.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        duration = now - created_at
        max_duration = timedelta(minutes=self.run_timeout_minutes)
        if duration > max_duration:
            msg = (
                f"WorkflowRun '{run.run_id}' đã chạy quá giới hạn RUN_TIMEOUT_MINUTES "
                f"({duration.total_seconds() / 60:.1f} phút > {self.run_timeout_minutes} phút)."
            )
            return False, msg
        return True, None

    def check_task_attempts(self, task: TaskModel) -> Tuple[bool, Optional[str]]:
        """Kiểm tra số lần thử lại của một task đơn lẻ."""
        max_att = task.max_attempts or DEFAULT_MAX_ATTEMPTS
        if task.attempts >= max_att:
            msg = f"Task '{task.task_id}' đã sử dụng hết số lần thử lại cho phép ({task.attempts}/{max_att})."
            return False, msg
        return True, None

    def check_run_retry_budget(self, session: Session, run_id: str) -> Tuple[bool, Optional[str]]:
        """Kiểm tra ngân sách retry cho toàn bộ run (tổng số lần retry không vượt quá MAX_RUN_RETRIES)."""
        max_retries = getattr(settings, "MAX_RUN_RETRIES", DEFAULT_MAX_RUN_RETRIES)
        stmt = select(func.sum(TaskModel.attempts), func.count(TaskModel.task_id)).where(TaskModel.run_id == run_id)
        row = session.execute(stmt).first()
        total_attempts = (row[0] or 0) if row else 0
        total_tasks = (row[1] or 0) if row else 0
        # Số lần retry = tổng số attempts - số lượng tasks (vì attempt 1 là lượt chạy gốc)
        total_retries = max(0, total_attempts - total_tasks)

        if total_retries >= max_retries:
            msg = f"Đã vượt quá ngân sách retry của WorkflowRun ({total_retries} >= {max_retries} retries). Chuyển task sang BLOCKED để bảo vệ hệ thống."
            return False, msg
        return True, None

    def is_poison_task(self, task: TaskModel) -> bool:
        """Phát hiện task độc hại (liên tục lỗi, đạt hoặc vượt max_attempts)."""
        max_att = task.max_attempts or DEFAULT_MAX_ATTEMPTS
        return task.attempts >= max_att

    def check_prod_deploy_permission(self, gate_approved: bool) -> Tuple[bool, Optional[str]]:
        """Kiểm tra điều kiện triển khai môi trường Production."""
        allow_prod = getattr(settings, "ALLOW_PROD_DEPLOY", False)
        if not allow_prod:
            return False, "Triển khai Production bị vô hiệu hóa bởi cấu hình hệ thống (ALLOW_PROD_DEPLOY=False)."
        if not gate_approved:
            return False, "Cổng phê duyệt Production (approve_prod) chưa được duyệt bởi người quản trị."
        return True, None

    def enforce_guards(
        self,
        session: Session,
        run_id: str,
        fix_loop_count: int,
    ) -> Tuple[bool, Optional[str]]:
        """Kiểm tra toàn bộ các quy tắc bảo vệ đối với một run.

        Trả về (True, None) nếu hợp lệ; trả về (False, reason) nếu vi phạm.
        """
        # 1. Kiểm tra fix loop
        ok, msg = self.check_fix_loops(fix_loop_count)
        if not ok:
            return False, msg

        # 2. Kiểm tra task count
        ok, msg = self.check_task_count(session, run_id)
        if not ok:
            return False, msg

        # 3. Kiểm tra timeout
        run = session.get(WorkflowRunModel, run_id)
        if run:
            ok, msg = self.check_run_timeout(run)
            if not ok:
                return False, msg

        return True, None
