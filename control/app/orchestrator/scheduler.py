"""scheduler.py - Lập lịch theo đồ thị phụ thuộc (DAG) và điều phối công bằng đa run (Prompt S9).

Nguyên tắc:
- Không phải chuỗi tuyến tính: tính toán mọi task READY từ DB (mọi phụ thuộc đã COMPLETED).
- Tôn trọng MAX_PARALLEL_TASKS và tối đa 1 task đồng thời cho mỗi agent.
- Đa run chạy đồng thời, cô lập dữ liệu hoàn toàn.
- Chia lượt công bằng (Fair Scheduling / Round-Robin) giữa các run khi tranh chấp agent hoặc slot chạy.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, Priority
from agent_contract.system_models import DispatchState, RunStatus
from control.app.config import settings
from control.app.database.models import (
    AgentModel,
    TaskDependencyModel,
    TaskModel,
    WorkflowRunModel,
    utc_now,
)

logger = logging.getLogger(__name__)


class SchedulerService:
    """Dịch vụ tính toán lập lịch tác vụ độc lập và công bằng trên PC1 Control Node."""

    def __init__(self, max_parallel_tasks: Optional[int] = None) -> None:
        self.max_parallel_tasks = max_parallel_tasks or getattr(settings, "MAX_PARALLEL_TASKS", 2)

    def count_active_tasks_globally(self, session: Session) -> int:
        """Đếm tổng số task đang chạy (ASSIGNED hoặc RUNNING) trên toàn bộ hệ thống."""
        stmt = select(func.count(TaskModel.task_id)).where(
            TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value])
        )
        return session.scalar(stmt) or 0

    def get_busy_agents(self, session: Session) -> Set[str]:
        """Lấy danh sách các agent hiện đang bận (có current_task hoặc đang có task ASSIGNED/RUNNING)."""
        busy_agents: Set[str] = set()

        # 1. Từ bảng agents (current_task is not null)
        stmt_agents = select(AgentModel.agent_id).where(AgentModel.current_task.isnot(None))
        for aid in session.execute(stmt_agents).scalars().all():
            busy_agents.add(aid)

        # 2. Từ bảng tasks (ASSIGNED hoặc RUNNING)
        stmt_tasks = select(TaskModel.agent_id).where(
            TaskModel.dispatch_state.in_([DispatchState.ASSIGNED.value, DispatchState.RUNNING.value])
        )
        for aid in session.execute(stmt_tasks).scalars().all():
            busy_agents.add(aid)

        return busy_agents

    def get_ready_tasks_for_run(self, session: Session, run_id: str) -> List[TaskModel]:
        """Lấy danh sách các task READY cho một run cụ thể:

        1. Run phải có trạng thái RUNNING.
        2. Task có dispatch_state là QUEUED hoặc (RETRY_WAIT và updated_at <= now).
        3. Mọi phụ thuộc trong task_dependencies đều có dispatch_state == COMPLETED.
        4. Bỏ qua task có role == GATE (cần phê duyệt người dùng).
        """
        run = session.get(WorkflowRunModel, run_id)
        if not run or run.status != RunStatus.RUNNING.value:
            return []

        now = utc_now()

        # Subquery: các phụ thuộc CHƯA COMPLETED
        uncompleted_deps = (
            select(TaskDependencyModel.task_id)
            .join(TaskModel, TaskModel.task_id == TaskDependencyModel.depends_on_task_id)
            .where(TaskModel.dispatch_state != DispatchState.COMPLETED.value)
        )

        stmt = (
            select(TaskModel)
            .where(TaskModel.run_id == run_id)
            .where(
                (TaskModel.dispatch_state == DispatchState.QUEUED.value)
                | (
                    (TaskModel.dispatch_state == DispatchState.RETRY_WAIT.value)
                    & (TaskModel.updated_at <= now)
                )
            )
            .where(~TaskModel.task_id.in_(uncompleted_deps))
            .where(TaskModel.role != "GATE")
            .where(~TaskModel.action.startswith("approve_"))
            .order_by(TaskModel.created_at.asc())
        )

        candidates = list(session.execute(stmt).scalars().all())
        ready_tasks = []
        for cand in candidates:
            if cand.action == "deploy":
                from control.app.services.deploy_guard import DeployGuardService
                ok_t, _ = DeployGuardService.check_tester_pass(session, run_id)
                ok_s, _ = DeployGuardService.check_security_pass(session, run_id)
                if not ok_t or not ok_s:
                    continue
            ready_tasks.append(cand)
        return ready_tasks

    def get_fair_scheduled_tasks(
        self,
        session: Session,
        target_run_id: Optional[str] = None,
    ) -> List[TaskModel]:
        """Thuật toán lập lịch công bằng (Fair Scheduling) đa run theo Round-Robin:

        - Thu thập task READY của tất cả các run đang RUNNING.
        - Chia lượt lần lượt từng run một task.
        - Đảm bảo không vượt quá giới hạn MAX_PARALLEL_TASKS toàn cục.
        - Đảm bảo mỗi agent chỉ nhận tối đa 1 task đồng thời.
        """
        active_count = self.count_active_tasks_globally(session)
        available_slots = max(0, self.max_parallel_tasks - active_count)
        if available_slots <= 0:
            logger.debug(f"Đã đầy giới hạn MAX_PARALLEL_TASKS ({active_count}/{self.max_parallel_tasks}).")
            return []

        busy_agents = self.get_busy_agents(session)

        # 1. Tìm các run đang hoạt động (RUNNING)
        if target_run_id:
            active_runs = [target_run_id]
        else:
            stmt = select(WorkflowRunModel.run_id).where(
                WorkflowRunModel.status == RunStatus.RUNNING.value
            ).order_by(WorkflowRunModel.created_at.asc())
            active_runs = list(session.execute(stmt).scalars().all())

        if not active_runs:
            return []

        # 2. Gom danh sách task READY theo từng run
        run_ready_map: Dict[str, List[TaskModel]] = {}
        for rid in active_runs:
            tasks = self.get_ready_tasks_for_run(session, rid)
            if tasks:
                run_ready_map[rid] = tasks

        if not run_ready_map:
            return []

        # 3. Phân bổ công bằng theo Round-Robin
        scheduled_tasks: List[TaskModel] = []
        allocated_agents = set(busy_agents)

        runs_list = list(run_ready_map.keys())
        run_indices = {rid: 0 for rid in runs_list}

        progress = True
        while progress and len(scheduled_tasks) < available_slots:
            progress = False
            for rid in runs_list:
                if len(scheduled_tasks) >= available_slots:
                    break

                tasks = run_ready_map[rid]
                idx = run_indices[rid]

                # Tìm task kế tiếp của run này mà agent chưa bận
                while idx < len(tasks):
                    candidate = tasks[idx]
                    idx += 1
                    agent_id = candidate.agent_id
                    if agent_id not in allocated_agents:
                        # Chọn task này
                        scheduled_tasks.append(candidate)
                        allocated_agents.add(agent_id)
                        progress = True
                        break

                run_indices[rid] = idx

        return scheduled_tasks

    def inspect_run_state(self, session: Session, run_id: str) -> Dict[str, Any]:
        """Kiểm tra chi tiết trạng thái tổng thể các task của một WorkflowRun."""
        tasks = session.execute(
            select(TaskModel).where(TaskModel.run_id == run_id)
        ).scalars().all()

        total = len(tasks)
        counts: Dict[str, int] = defaultdict(int)
        for t in tasks:
            counts[t.dispatch_state] += 1

        completed = counts[DispatchState.COMPLETED.value]
        failed = counts[DispatchState.FAILED.value]
        blocked = counts[DispatchState.BLOCKED.value]
        active = counts[DispatchState.ASSIGNED.value] + counts[DispatchState.RUNNING.value]
        queued = counts[DispatchState.QUEUED.value] + counts[DispatchState.RETRY_WAIT.value]

        is_all_done = (total > 0 and completed == total)
        has_blocker = (blocked > 0)
        has_failure = (failed > 0)

        return {
            "total_tasks": total,
            "completed_count": completed,
            "failed_count": failed,
            "blocked_count": blocked,
            "active_count": active,
            "queued_count": queued,
            "is_all_completed": is_all_done,
            "has_blocked": has_blocker,
            "has_failed": has_failure,
            "state_counts": dict(counts),
        }
