"""nodes.py - Định nghĩa toàn bộ 16 node xử lý luồng thực thi trong LangGraph Orchestrator (Prompt S9).

Danh sách các node:
1. receive_request: Tiếp nhận request, xác thực, khởi tạo trạng thái run.
2. pm_planning: Lập kế hoạch phát triển (gọi PM Agent hoặc fallback).
3. create_linear_project: Đưa thông tin dự án, milestone, tasks vào hàng đợi Linear.
4. await_plan_approval: Chờ phê duyệt kế hoạch nếu APPROVAL_MODE=True.
5. schedule: Lập lịch phụ thuộc DAG, tìm các task READY.
6. dispatch_ready: Giao việc cho các agent/worker (tôn trọng MAX_PARALLEL_TASKS & 1 task/agent).
7. wait_for_event: Lắng nghe sự kiện từ Redis Streams (timeout, không giữ HTTP).
8. process_result: Xử lý kết quả task vừa nộp về, mở rộng user story nếu cần.
9. route: Đánh giá và xác định nhánh rẽ tiếp theo.
10. retry_task: Thử lại task thất bại với backoff.
11. handle_failure: Xử lý lỗi, dừng run an toàn, ghi audit log.
12. fix_loop: Vòng sửa lỗi tự động (TESTER FAIL, SECURITY FAIL, Build FAIL).
13. gate: Cổng kiểm soát người dùng (approve_scope, approve_prod).
14. devops_deploy: Triển khai staging tự động, production chỉ khi được phép và đã duyệt.
15. health_check: Kiểm định sức khỏe dịch vụ sau khi deploy.
16. finalize: Hoàn tất toàn bộ Workflow Run, nghiệm thu và kết thúc.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, ArtifactType, ErrorCode
from agent_contract.system_models import DispatchState, RunStatus, SystemEventType
from control.app.config import settings
from control.app.database.models import (
    ApprovalModel,
    TaskDependencyModel,
    TaskModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import (
    ApprovalRepository,
    AuditRepository,
    TaskRepository,
    WorkflowRunRepository,
)
from control.app.database.session import get_session_factory
from control.app.orchestrator.guards import WorkflowGuards
from control.app.orchestrator.scheduler import SchedulerService
from control.app.orchestrator.state import WorkflowState
from control.app.queue.events import EventPublisher, get_event_publisher
from control.app.queue.redis import get_redis_client
from control.app.services.approval import ApprovalService
from control.app.services.dispatcher import DispatcherService
from control.app.services.plan_service import PlanService, build_default_plan
from control.app.services.task_manager import TaskManager

logger = logging.getLogger(__name__)


def _get_session() -> Session:
    """Hàm tiện ích mở SQLAlchemy session."""
    factory = get_session_factory()
    return factory()


# -----------------------------------------------------------------------------
# 1. RECEIVE_REQUEST
# -----------------------------------------------------------------------------
def receive_request_node(state: WorkflowState) -> Dict[str, Any]:
    """Khởi tạo trạng thái, đọc thông tin từ DB, chuẩn bị cho lập kế hoạch."""
    run_id = state["run_id"]
    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return {
                "error_message": f"Không tìm thấy WorkflowRun '{run_id}' trong DB.",
                "is_failed": True,
                "next_node": "handle_failure",
            }

        project_id = run.project_id
        brief = run.brief

        if run.status == RunStatus.CREATED.value:
            run.status = RunStatus.PLANNING.value
            run.current_stage = "scoping"
            run.updated_at = utc_now()
            session.commit()

    return {
        "project_id": project_id,
        "brief": brief,
        "fix_loop_count": state.get("fix_loop_count", 0),
        "stop_requested": False,
        "is_paused": False,
        "current_stage": "scoping",
        "next_node": "pm_planning",
    }


# -----------------------------------------------------------------------------
# 2. PM_PLANNING
# -----------------------------------------------------------------------------
def pm_planning_node(state: WorkflowState) -> Dict[str, Any]:
    """Thực thi lập kế hoạch tổng thể qua PlanService nếu chưa có kế hoạch trong DB."""
    run_id = state["run_id"]
    brief = state.get("brief", "")
    project_id = state.get("project_id", "")

    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return {"error_message": f"Run '{run_id}' không tồn tại.", "is_failed": True, "next_node": "handle_failure"}

        # Nếu đã có tasks trong DB cho run này rồi thì không sinh lại
        stmt = select(TaskModel.task_id).where(TaskModel.run_id == run_id)
        existing_tasks = session.execute(stmt).scalars().all()

        if not existing_tasks:
            pub = get_event_publisher()
            plan_svc = PlanService(publisher=pub)
            plan_svc.create_project_plan(
                session=session,
                run_id=run_id,
                prompt=brief,
                project_id=project_id,
            )

    return {
        "current_stage": "planning_completed",
        "next_node": "create_linear_project",
    }


# -----------------------------------------------------------------------------
# 3. CREATE_LINEAR_PROJECT
# -----------------------------------------------------------------------------
def create_linear_project_node(state: WorkflowState) -> Dict[str, Any]:
    """Đảm bảo các thực thể của kế hoạch đã được đưa vào hàng đợi đồng bộ Linear."""
    # Việc enqueue đã được PlanService tích hợp sẵn an toàn.
    return {
        "current_stage": "linear_synced",
        "next_node": "await_plan_approval",
    }


# -----------------------------------------------------------------------------
# 4. AWAIT_PLAN_APPROVAL
# -----------------------------------------------------------------------------
def await_plan_approval_node(state: WorkflowState) -> Dict[str, Any]:
    """Kiểm tra chế độ phê duyệt APPROVAL_MODE."""
    run_id = state["run_id"]

    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return {"error_message": f"Run '{run_id}' không tồn tại.", "is_failed": True, "next_node": "handle_failure"}

        # Nếu APPROVAL_MODE bật và run đang ở AWAITING_APPROVAL
        if getattr(settings, "APPROVAL_MODE", True) and run.status == RunStatus.AWAITING_APPROVAL.value:
            logger.info(f"Run '{run_id}' đang ở trạng thái AWAITING_APPROVAL. Chờ người dùng phê duyệt...")
            return {
                "current_stage": "awaiting_plan_approval",
                "next_node": "wait_for_event",
            }

        # Nếu run đã RUNNING
        if run.status == RunStatus.RUNNING.value:
            return {
                "current_stage": "running",
                "next_node": "wait_for_event",
            }

        if run.status in (RunStatus.FAILED.value, RunStatus.CANCELLED.value, RunStatus.BLOCKED.value):
            return {
                "stop_requested": True,
                "next_node": "handle_failure",
            }

    return {"next_node": "schedule"}


# -----------------------------------------------------------------------------
# 5. SCHEDULE
# -----------------------------------------------------------------------------
def schedule_node(state: WorkflowState) -> Dict[str, Any]:
    """Kiểm tra các quy tắc bảo vệ (Guards) và tính toán danh sách task READY."""
    run_id = state["run_id"]
    fix_loops = state.get("fix_loop_count", 0)

    with _get_session() as session:
        guards = WorkflowGuards()
        ok, reason = guards.enforce_guards(session, run_id, fix_loops)
        if not ok:
            logger.warning(f"Run '{run_id}' vi phạm Guard: {reason}. Chuyển sang BLOCKED.")
            run = session.get(WorkflowRunModel, run_id)
            if run:
                run.status = RunStatus.BLOCKED.value
                session.commit()
            return {
                "error_message": reason,
                "is_blocked": True,
                "next_node": "handle_failure",
            }

        scheduler = SchedulerService()
        inspect = scheduler.inspect_run_state(session, run_id)

        # 1. Nếu toàn bộ task đã COMPLETED -> finalize
        if inspect["is_all_completed"]:
            return {
                "is_completed": True,
                "current_stage": "all_completed",
                "next_node": "finalize",
            }

        # 2. Kiểm tra xem có task GATE nào đang READY (phụ thuộc đã xong và task chưa COMPLETED)
        stmt_gate = select(TaskModel).where(
            TaskModel.run_id == run_id,
            TaskModel.dispatch_state != DispatchState.COMPLETED.value,
        )
        gate_tasks = session.execute(stmt_gate).scalars().all()
        for gt in gate_tasks:
            if gt.role == "GATE" or gt.action.startswith("approve_"):
                dep_ids = session.execute(
                    select(TaskDependencyModel.depends_on_task_id).where(
                        TaskDependencyModel.task_id == gt.task_id
                    )
                ).scalars().all()
                if not dep_ids:
                    gate_ready = True
                else:
                    completed_deps = session.execute(
                        select(TaskModel.task_id).where(
                            TaskModel.task_id.in_(dep_ids),
                            TaskModel.dispatch_state == DispatchState.COMPLETED.value,
                        )
                    ).scalars().all()
                    gate_ready = (len(completed_deps) == len(dep_ids))

                if gate_ready:
                    logger.info(f"Phát hiện cổng kiểm soát GATE '{gt.task_id}' (action={gt.action}) sẵn sàng. Chuyển sang gate_node.")
                    return {
                        "ready_tasks_count": 0,
                        "active_tasks_count": inspect["active_count"],
                        "completed_tasks_count": inspect["completed_count"],
                        "total_tasks_count": inspect["total_tasks"],
                        "current_stage": "gate_ready",
                        "next_node": "gate",
                    }

        # 3. Lấy các task READY
        ready_tasks = scheduler.get_ready_tasks_for_run(session, run_id)

    return {
        "ready_tasks_count": len(ready_tasks),
        "active_tasks_count": inspect["active_count"],
        "completed_tasks_count": inspect["completed_count"],
        "total_tasks_count": inspect["total_tasks"],
        "current_stage": "scheduled",
        "next_node": "dispatch_ready" if ready_tasks else "end",
    }


# -----------------------------------------------------------------------------
# 6. DISPATCH_READY
# -----------------------------------------------------------------------------
def dispatch_ready_node(state: WorkflowState, config: RunnableConfig) -> Dict[str, Any]:
    """Giao các task READY cho agent/worker tôn trọng giới hạn song song và cô lập 1 task/agent."""
    run_id = state["run_id"]
    factory = get_session_factory()
    redis_client = get_redis_client()
    pub = get_event_publisher(redis_client)

    configurable = (config or {}).get("configurable", {})
    dispatcher = configurable.get("dispatcher")
    if dispatcher is None:
        dispatcher = DispatcherService(
            session_factory=factory,
            redis_client=redis_client,
            publisher=pub,
        )

    dispatched = 0
    with factory() as session:
        # Sử dụng scheduler để lấy task theo cơ chế công bằng (Fair Scheduling)
        scheduler = SchedulerService()
        tasks_to_dispatch = scheduler.get_fair_scheduled_tasks(session, target_run_id=run_id)

        for task in tasks_to_dispatch:
            success = dispatcher.dispatch_single_task(session, task)
            if success:
                dispatched += 1

    return {
        "current_stage": "dispatched",
        "next_node": "wait_for_event",
    }


# -----------------------------------------------------------------------------
# 7. WAIT_FOR_EVENT
# -----------------------------------------------------------------------------
def wait_for_event_node(state: WorkflowState, config: RunnableConfig) -> Dict[str, Any]:
    """Chờ sự kiện từ Redis Streams hoặc kiểm tra thay đổi trong DB (timeout an toàn)."""
    run_id = state["run_id"]
    configurable = (config or {}).get("configurable", {})
    dispatcher = configurable.get("dispatcher")
    redis_client = configurable.get("redis_client") or (dispatcher.redis_client if dispatcher else None) or get_redis_client()

    processed = set(state.get("processed_task_ids") or [])
    if state.get("last_processed_task_id"):
        processed.add(state["last_processed_task_id"])

    event_payload: Optional[Dict[str, Any]] = None

    if redis_client:
        stream_key = f"events:run:{run_id}"
        try:
            # Đọc từ stream (lấy các tin nhắn mới nhất)
            raw = redis_client.xrevrange(stream_key, count=20)
            if raw:
                for msg_id, fields in raw:
                    val = fields.get("payload") or fields.get(b"payload")
                    if isinstance(val, bytes):
                        val = val.decode("utf-8")
                    parsed = {}
                    if isinstance(val, str):
                        try:
                            parsed = json.loads(val)
                        except Exception:
                            parsed = {"raw": val}
                    elif isinstance(val, dict):
                        parsed = val

                    ev_tid = parsed.get("task_id") or fields.get("task_id") or fields.get(b"task_id")
                    if isinstance(ev_tid, bytes):
                        ev_tid = ev_tid.decode("utf-8")
                    if ev_tid and ev_tid not in processed:
                        event_payload = parsed
                        if "task_id" not in event_payload:
                            event_payload["task_id"] = ev_tid
                        break
        except Exception as e:
            logger.debug(f"Đọc Redis stream gặp lỗi ({e}). Tiếp tục kiểm tra DB.")

    # Đọc DB để xác nhận trạng thái tức thời
    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if run and run.status == RunStatus.CANCELLED.value:
            return {"stop_requested": True, "next_node": "handle_failure"}
        if run and run.status == "PAUSED":
            return {"is_paused": True, "next_node": "end"}

        # Nếu chưa có event từ Redis, kiểm tra trực tiếp từ DB xem có task nào vừa kết thúc chưa được process
        if not event_payload:
            stmt_unproc = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state.in_([
                    DispatchState.COMPLETED.value,
                    DispatchState.FAILED.value,
                    DispatchState.BLOCKED.value,
                    DispatchState.RETRY_WAIT.value,
                ]),
            )
            if processed:
                stmt_unproc = stmt_unproc.where(TaskModel.task_id.not_in(list(processed)))
            stmt_unproc = stmt_unproc.order_by(TaskModel.updated_at.desc()).limit(1)
            unproc_task = session.execute(stmt_unproc).scalars().first()
            if unproc_task:
                event_payload = {
                    "task_id": unproc_task.task_id,
                    "status": unproc_task.dispatch_state,
                    "result": unproc_task.result,
                }

    if event_payload:
        return {
            "last_event": event_payload,
            "current_stage": "event_received",
            "next_node": "process_result",
        }

    return {
        "current_stage": "waiting_event",
        "next_node": "schedule",
    }


# -----------------------------------------------------------------------------
# 8. PROCESS_RESULT
# -----------------------------------------------------------------------------
def process_result_node(state: WorkflowState) -> Dict[str, Any]:
    """Xử lý kết quả task vừa hoàn tất và quyết định nhánh rẽ kế tiếp."""
    run_id = state["run_id"]
    next_step = "schedule"
    last_task_id: Optional[str] = None

    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return {"error_message": f"Run '{run_id}' không tồn tại.", "next_node": "handle_failure"}

        # Kiểm tra nếu run đang ở trạng thái AWAITING_APPROVAL
        if run.status == RunStatus.AWAITING_APPROVAL.value:
            # Kiểm tra xem có cổng nào vừa được duyệt không
            stmt_appr = select(ApprovalModel).where(
                ApprovalModel.run_id == run_id,
                ApprovalModel.status == "APPROVED",
            ).order_by(ApprovalModel.decided_at.desc())
            latest_appr = session.execute(stmt_appr).scalars().first()
            if latest_appr:
                # Run đã được duyệt qua API / Linear
                run.status = RunStatus.RUNNING.value
                session.commit()
                return {"current_stage": "approved_resumed", "next_node": "schedule"}
            else:
                return {"current_stage": "still_awaiting", "next_node": "wait_for_event"}

        # 1. Tìm task vừa hoàn tất gần nhất (COMPLETED, FAILED hoặc BLOCKED)
        event_task_id = None
        if state.get("last_event"):
            ev = state["last_event"]
            if isinstance(ev, dict):
                event_task_id = ev.get("task_id") or ev.get("payload", {}).get("task_id")

        if event_task_id:
            task = session.get(TaskModel, event_task_id)
        else:
            stmt_task = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state.in_([
                    DispatchState.COMPLETED.value,
                    DispatchState.FAILED.value,
                    DispatchState.BLOCKED.value,
                    DispatchState.RETRY_WAIT.value,
                ]),
            ).order_by(TaskModel.updated_at.desc()).limit(1)
            task = session.execute(stmt_task).scalars().first()
            if not task:
                stmt_fallback = select(TaskModel).where(TaskModel.run_id == run_id).order_by(TaskModel.updated_at.desc()).limit(1)
                task = session.execute(stmt_fallback).scalars().first()

        if task:
            last_task_id = task.task_id
            # Nếu task này đã được xử lý ở chu kỳ trước rồi và không có event mới cho nó -> không xử lý lại
            if last_task_id == state.get("last_processed_task_id") and not event_task_id:
                return {
                    "last_processed_task_id": last_task_id,
                    "next_node": "schedule",
                    "current_stage": "idle_waiting",
                }

            state_val = task.dispatch_state
            action = task.action

            # a. Mở rộng User Stories khi task ba_write_user_stories hoàn thành
            if action == "write_user_stories" and state_val == DispatchState.COMPLETED.value:
                # Kiểm tra xem stories đã được mở rộng chưa
                expanded_check = session.execute(
                    select(TaskModel.task_id).where(
                        TaskModel.run_id == run_id,
                        TaskModel.action == "implement_feature",
                    )
                ).scalars().first()

                if not expanded_check:
                    # Đọc artifact USER_STORY từ kết quả task
                    res_json = task.result or {}
                    stories_data = res_json.get("output_artifacts") or res_json.get("data") or []
                    plan_svc = PlanService()
                    plan_svc.expand_stories(session, run_id=run_id, stories_artifact_or_data=stories_data)
                    logger.info(f"Đã mở rộng User Stories cho run '{run_id}'.")
                    next_step = "schedule"

            # b. Kiểm tra các Gate task (approve_scope, approve_prod)
            elif (task.role == "GATE" or action.startswith("approve_")) and state_val != DispatchState.COMPLETED.value:
                next_step = "gate"

            # c. TESTER verdict FAIL -> fix_loop
            elif task.role == "TESTER" and action == "run_test_suite":
                res_dict = task.result or {}
                verdict = str(
                    res_dict.get("verdict")
                    or res_dict.get("metadata", {}).get("verdict")
                    or res_dict.get("data", {}).get("verdict")
                    or ""
                ).upper()
                if verdict == "FAIL" or state_val == DispatchState.FAILED.value:
                    logger.info(f"TESTER verdict FAIL tại task '{task.task_id}'. Chuyển nhánh fix_loop.")
                    next_step = "fix_loop"

            # d. SECURITY verdict FAIL -> fix_loop
            elif task.role == "SECURITY" and action == "security_review":
                res_dict = task.result or {}
                verdict = str(
                    res_dict.get("verdict")
                    or res_dict.get("metadata", {}).get("verdict")
                    or res_dict.get("data", {}).get("verdict")
                    or ""
                ).upper()
                findings = (
                    res_dict.get("findings")
                    or res_dict.get("metadata", {}).get("findings")
                    or res_dict.get("data", {}).get("findings")
                    or []
                )
                has_critical_high = any(
                    str(f.get("severity", "")).upper() in ("HIGH", "CRITICAL") for f in findings if isinstance(f, dict)
                )
                if verdict == "FAIL" or has_critical_high or state_val == DispatchState.FAILED.value:
                    logger.info(f"SECURITY verdict FAIL / High findings tại task '{task.task_id}'. Chuyển nhánh fix_loop.")
                    next_step = "fix_loop"

            # e. Build FAIL -> fix_loop
            elif action == "build_image" and state_val == DispatchState.FAILED.value:
                logger.info(f"Docker Build FAIL tại task '{task.task_id}'. Chuyển nhánh fix_loop.")
                next_step = "fix_loop"

            # f. Health check FAIL
            elif action == "health_check" and state_val == DispatchState.FAILED.value:
                next_step = "health_check"

            # g. Task bị BLOCKED với MISSING_CONTEXT
            elif state_val == DispatchState.BLOCKED.value:
                run.status = RunStatus.BLOCKED.value
                session.commit()
                logger.warning(f"Task '{task.task_id}' bị BLOCKED (MISSING_CONTEXT). Chuyển Run sang BLOCKED.")
                return {"is_blocked": True, "next_node": "wait_for_event"}

            # h. Task cần retry_wait
            elif state_val == DispatchState.RETRY_WAIT.value:
                next_step = "retry_task"

        # Kiểm tra nếu toàn bộ hoàn thành
        if next_step not in ("fix_loop", "gate", "handle_failure", "retry_task", "devops_deploy", "health_check"):
            scheduler = SchedulerService()
            inspect = scheduler.inspect_run_state(session, run_id)
            if inspect["is_all_completed"]:
                next_step = "finalize"

    processed = list(state.get("processed_task_ids") or [])
    if last_task_id and last_task_id not in processed:
        processed.append(last_task_id)

    return {
        "last_processed_task_id": last_task_id,
        "processed_task_ids": processed,
        "last_event": None,
        "next_node": next_step,
        "current_stage": "result_processed",
    }


# -----------------------------------------------------------------------------
# 9. ROUTE
# -----------------------------------------------------------------------------
def route_node(state: WorkflowState) -> Dict[str, Any]:
    """Node định tuyến: xác định chính xác next_node theo luật tất định."""
    target = state.get("next_node") or "schedule"
    return {"next_node": target}


# -----------------------------------------------------------------------------
# 10. RETRY_TASK
# -----------------------------------------------------------------------------
def retry_task_node(state: WorkflowState) -> Dict[str, Any]:
    """Xử lý tác vụ chuẩn bị thử lại sau khi hết thời gian chờ backoff."""
    return {"next_node": "schedule", "current_stage": "task_retried"}


# -----------------------------------------------------------------------------
# 11. HANDLE_FAILURE
# -----------------------------------------------------------------------------
def handle_failure_node(state: WorkflowState) -> Dict[str, Any]:
    """Xử lý lỗi hệ thống hoặc vi phạm bảo vệ, chuyển trạng thái và dừng luồng."""
    run_id = state["run_id"]
    err = state.get("error_message") or "Quy trình dừng do lỗi hoặc bị hủy."

    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if run:
            if state.get("is_blocked"):
                run.status = RunStatus.BLOCKED.value
            elif state.get("stop_requested"):
                run.status = RunStatus.CANCELLED.value
            else:
                run.status = RunStatus.FAILED.value
            run.updated_at = utc_now()
            session.commit()

        AuditRepository.record(
            session=session,
            actor="ORCHESTRATOR",
            action="RUN_TERMINATED",
            target=run_id,
            run_id=run_id,
            result=run.status if run else "FAILED",
            details={"reason": err},
        )

    return {
        "is_failed": True,
        "current_stage": "terminated",
        "next_node": "end",
    }


# -----------------------------------------------------------------------------
# 12. FIX_LOOP
# -----------------------------------------------------------------------------
def fix_loop_node(state: WorkflowState) -> Dict[str, Any]:
    """Định tuyến vòng sửa lỗi tự động (TESTER FAIL, SECURITY FAIL, Build FAIL).

    - Tăng bộ đếm fix_loop_count.
    - Nếu fix_loop_count > MAX_FIX_LOOPS (3): chuyển sang BLOCKED, không lặp vô hạn.
    - Sinh task fix_bugs cho DEV và task re-test/re-review tương ứng.
    """
    run_id = state["run_id"]
    curr_loops = state.get("fix_loop_count", 0) + 1

    guards = WorkflowGuards()
    ok, reason = guards.check_fix_loops(curr_loops)
    if not ok:
        logger.warning(f"Run '{run_id}' vượt quá MAX_FIX_LOOPS ({curr_loops} > {guards.max_fix_loops}). Dừng luồng.")
        with _get_session() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run:
                run.status = RunStatus.BLOCKED.value
                session.commit()
            AuditRepository.record(
                session=session,
                actor="ORCHESTRATOR",
                action="MAX_FIX_LOOPS_EXCEEDED",
                target=run_id,
                run_id=run_id,
                result="BLOCKED",
                details={"fix_loops": curr_loops, "reason": reason},
            )
        return {
            "fix_loop_count": curr_loops,
            "error_message": reason,
            "is_blocked": True,
            "next_node": "handle_failure",
        }

    # Sinh task sửa lỗi tương ứng trong DB
    with _get_session() as session:
        # Tìm task gây lỗi gần nhất
        last_tid = state.get("last_processed_task_id")
        source_task = session.get(TaskModel, last_tid) if last_tid else None
        if not source_task:
            stmt_fail = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.dispatch_state.in_([DispatchState.FAILED.value, DispatchState.COMPLETED.value]),
            ).order_by(TaskModel.updated_at.desc())
            for cand in session.execute(stmt_fail).scalars().all():
                if cand.role in ("TESTER", "SECURITY") or cand.action in ("build_image", "health_check"):
                    source_task = cand
                    break

        if source_task:
            loop_tag = f"fix_loop_{curr_loops}_{uuid.uuid4().hex[:4]}"
            dev_task_id = f"{run_id}_dev_fix_{loop_tag}"

            if source_task.role == "TESTER":
                # TESTER FAIL -> DEV fix_bugs -> TESTER run_test_suite
                test_task_id = f"{run_id}_retest_{loop_tag}"
                new_tasks = [
                    {
                        "task_id": dev_task_id,
                        "role": "DEV",
                        "action": "fix_bugs",
                        "instruction": f"Khắc phục các lỗi kiểm thử trong test report (Vòng {curr_loops})",
                        "input_artifact_types": ["TEST_REPORT", "SOURCE_CODE"],
                        "output_artifact_type": "SOURCE_CODE",
                        "acceptance_criteria": ["Sửa toàn bộ ca kiểm thử thất bại"],
                    },
                    {
                        "task_id": test_task_id,
                        "role": "TESTER",
                        "action": "run_test_suite",
                        "instruction": f"Chạy lại bộ kiểm thử tự động sau khi sửa lỗi (Vòng {curr_loops})",
                        "input_artifact_types": ["SOURCE_CODE", "TEST_CASE"],
                        "output_artifact_type": "TEST_REPORT",
                        "acceptance_criteria": ["100% test cases PASS"],
                    },
                ]
                TaskManager.create_tasks_from_plan(
                    session=session,
                    run_id=run_id,
                    tasks_data=new_tasks,
                    dependencies=[(test_task_id, dev_task_id)],
                )

                # Nối lại các task phía sau (ví dụ: security_review) phụ thuộc vào test_task_id mới
                sec_task = session.execute(
                    select(TaskModel).where(TaskModel.run_id == run_id, TaskModel.action == "security_review")
                ).scalars().first()
                if sec_task:
                    # Xóa phụ thuộc cũ
                    session.execute(
                        delete(TaskDependencyModel).where(
                            TaskDependencyModel.task_id == sec_task.task_id,
                            TaskDependencyModel.depends_on_task_id == source_task.task_id,
                        )
                    )
                    TaskRepository.add_dependency(session, task_id=sec_task.task_id, depends_on_task_id=test_task_id)

            elif source_task.role == "SECURITY":
                # SECURITY FAIL -> DEV fix_bugs -> TESTER run_test_suite -> SECURITY security_review
                test_task_id = f"{run_id}_retest_sec_{loop_tag}"
                sec_task_id = f"{run_id}_resec_{loop_tag}"
                new_tasks = [
                    {
                        "task_id": dev_task_id,
                        "role": "DEV",
                        "action": "fix_bugs",
                        "instruction": f"Khắc phục các lỗ hổng an toàn bảo mật HIGH/CRITICAL (Vòng {curr_loops})",
                        "input_artifact_types": ["SECURITY_REPORT", "SOURCE_CODE"],
                        "output_artifact_type": "SOURCE_CODE",
                        "acceptance_criteria": ["Khắc phục 100% các phát hiện an toàn"],
                    },
                    {
                        "task_id": test_task_id,
                        "role": "TESTER",
                        "action": "run_test_suite",
                        "instruction": f"Kiểm thử hồi quy sau khi vá bảo mật (Vòng {curr_loops})",
                        "input_artifact_types": ["SOURCE_CODE", "TEST_CASE"],
                        "output_artifact_type": "TEST_REPORT",
                        "acceptance_criteria": ["100% test cases PASS"],
                    },
                    {
                        "task_id": sec_task_id,
                        "role": "SECURITY",
                        "action": "security_review",
                        "instruction": f"Đánh giá lại an toàn bảo mật sau khi vá (Vòng {curr_loops})",
                        "input_artifact_types": ["SOURCE_CODE", "ARCHITECTURE"],
                        "output_artifact_type": "SECURITY_REPORT",
                        "acceptance_criteria": ["Không còn lỗ hổng mức độ HIGH hoặc CRITICAL"],
                    },
                ]
                TaskManager.create_tasks_from_plan(
                    session=session,
                    run_id=run_id,
                    tasks_data=new_tasks,
                    dependencies=[
                        (test_task_id, dev_task_id),
                        (sec_task_id, test_task_id),
                    ],
                )
                # Nối lại devops_generate_ci_config phụ thuộc vào sec_task_id mới
                ci_task = session.execute(
                    select(TaskModel).where(TaskModel.run_id == run_id, TaskModel.action == "generate_ci_config")
                ).scalars().first()
                if ci_task:
                    session.execute(
                        delete(TaskDependencyModel).where(
                            TaskDependencyModel.task_id == ci_task.task_id,
                            TaskDependencyModel.depends_on_task_id == source_task.task_id,
                        )
                    )
                    TaskRepository.add_dependency(session, task_id=ci_task.task_id, depends_on_task_id=sec_task_id)

            elif source_task.action == "build_image":
                # Build FAIL -> DEV sửa lỗi Dockerfile/Code -> build lại
                rebuild_task_id = f"{run_id}_rebuild_{loop_tag}"
                new_tasks = [
                    {
                        "task_id": dev_task_id,
                        "role": "DEV",
                        "action": "fix_bugs",
                        "instruction": f"Sửa lỗi mã nguồn hoặc cấu hình build Docker (Vòng {curr_loops})",
                        "input_artifact_types": ["DEPLOYMENT", "SOURCE_CODE"],
                        "output_artifact_type": "SOURCE_CODE",
                        "acceptance_criteria": ["Sửa cấu hình để build thành công"],
                    },
                    {
                        "task_id": rebuild_task_id,
                        "role": "DEVOPS",
                        "action": "build_image",
                        "instruction": f"Đóng gói container image lại sau khi DEV sửa (Vòng {curr_loops})",
                        "input_artifact_types": ["SOURCE_CODE", "DEPLOYMENT"],
                        "output_artifact_type": "DEPLOYMENT",
                        "acceptance_criteria": ["Build image thành công exit code 0"],
                    },
                ]
                TaskManager.create_tasks_from_plan(
                    session=session,
                    run_id=run_id,
                    tasks_data=new_tasks,
                    dependencies=[(rebuild_task_id, dev_task_id)],
                )

            session.commit()

    return {
        "fix_loop_count": curr_loops,
        "current_stage": "fix_loop_scheduled",
        "next_node": "schedule",
    }


# -----------------------------------------------------------------------------
# 13. GATE
# -----------------------------------------------------------------------------
def gate_node(state: WorkflowState) -> Dict[str, Any]:
    """Cổng kiểm soát phê duyệt người dùng (approve_scope, approve_prod)."""
    run_id = state["run_id"]

    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if not run:
            return {"error_message": f"Run '{run_id}' không tồn tại.", "next_node": "handle_failure"}

        # Tìm task GATE chưa hoàn tất
        stmt_active_gate = select(TaskModel).where(
            TaskModel.run_id == run_id,
            TaskModel.dispatch_state != DispatchState.COMPLETED.value,
        )
        tasks = session.execute(stmt_active_gate).scalars().all()
        active_gate = next(
            (t for t in tasks if t.role == "GATE" or t.action.startswith("approve_")),
            None,
        )
        if not active_gate:
            return {"current_stage": "gate_approved", "next_node": "schedule"}

        # Kiểm tra xem gate này đã được duyệt trong ApprovalModel chưa
        gate_name = active_gate.action
        stmt = select(ApprovalModel).where(
            ApprovalModel.run_id == run_id,
        ).order_by(ApprovalModel.decided_at.desc())
        approvals = session.execute(stmt).scalars().all()

        matching_approvals = [
            a for a in approvals
            if a.gate and (a.gate.lower() == gate_name.lower() or a.gate.lower().endswith(gate_name.lower()))
        ]

        is_rejected = any(a.status == "REJECTED" for a in matching_approvals)
        if is_rejected:
            run.status = RunStatus.BLOCKED.value
            session.commit()
            return {"error_message": f"Cổng kiểm soát '{gate_name}' bị từ chối phê duyệt.", "next_node": "handle_failure"}

        is_approved = any(a.status == "APPROVED" for a in matching_approvals)
        if is_approved:
            active_gate.dispatch_state = DispatchState.COMPLETED.value
            session.commit()
            return {"current_stage": "gate_approved", "next_node": "schedule"}

        # Nếu chưa được duyệt -> dừng lại chờ duyệt
        run.status = RunStatus.AWAITING_APPROVAL.value
        session.commit()

    return {"current_stage": "gate_waiting", "next_node": "end"}


# -----------------------------------------------------------------------------
# 14. DEVOPS_DEPLOY
# -----------------------------------------------------------------------------
def devops_deploy_node(state: WorkflowState) -> Dict[str, Any]:
    """Kiểm tra điều kiện triển khai Staging / Production."""
    run_id = state["run_id"]
    with _get_session() as session:
        from control.app.services.deploy_guard import DeployGuardService
        ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, run_id)
        if not ok:
            logger.warning(f"Điều kiện deploy không thỏa mãn cho run '{run_id}': {reason}")
            return {
                "error_message": f"Điều kiện deploy không thỏa mãn: {reason}",
                "is_blocked": True,
                "next_node": "handle_failure",
            }
    return {"current_stage": "deploy_ready", "next_node": "schedule"}


# -----------------------------------------------------------------------------
# 15. HEALTH_CHECK
# -----------------------------------------------------------------------------
def health_check_node(state: WorkflowState) -> Dict[str, Any]:
    """Kiểm định sức khỏe sau deploy. Nếu fail, chuyển nhánh sửa lỗi hoặc chặn run. Nếu thành công, cập nhật Linear."""
    run_id = state["run_id"]
    with _get_session() as session:
        stmt = select(TaskModel).where(
            TaskModel.run_id == run_id,
            TaskModel.action == "health_check",
        ).order_by(TaskModel.updated_at.desc())
        hc_task = session.execute(stmt).scalars().first()

        if hc_task and hc_task.dispatch_state == DispatchState.FAILED.value:
            # Health check thất bại -> định tuyến fix_loop
            return {"next_node": "fix_loop"}

        if hc_task and hc_task.dispatch_state == DispatchState.COMPLETED.value:
            # Lấy thông tin URL và version từ deploy task
            stmt_dep = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.action == "deploy",
            ).order_by(TaskModel.updated_at.desc())
            dep_task = session.execute(stmt_dep).scalars().first()
            dep_url = "http://127.0.0.1:8100"
            version = "v1.0"
            if dep_task and dep_task.result:
                for out in dep_task.result.get("outputs", []):
                    if isinstance(out, dict) and out.get("deployed_url"):
                        dep_url = out["deployed_url"]
                        version = out.get("version", version)

            tracker = state.get("linear_tracker")
            if not tracker:
                try:
                    from control.app.integrations.linear.client import LinearClient
                    if not getattr(settings, "LINEAR_DRY_RUN", True):
                        tracker = LinearClient()
                except Exception:
                    tracker = None

            from control.app.services.deploy_guard import DeployGuardService
            DeployGuardService.post_deployment_to_linear(
                session=session,
                run_id=run_id,
                deployed_url=dep_url,
                version=version,
                status="HEALTHY",
                tracker=tracker,
                task_id=hc_task.task_id,
            )

    return {"next_node": "schedule"}


# -----------------------------------------------------------------------------
# 16. FINALIZE
# -----------------------------------------------------------------------------
def finalize_node(state: WorkflowState) -> Dict[str, Any]:
    """Hoàn tất toàn bộ WorkflowRun khi mọi tác vụ đã hoàn thành xuất sắc."""
    run_id = state["run_id"]
    now = utc_now()

    with _get_session() as session:
        run = session.get(WorkflowRunModel, run_id)
        if run:
            run.status = RunStatus.COMPLETED.value
            run.completed_at = now
            run.updated_at = now
            session.commit()

        AuditRepository.record(
            session=session,
            actor="ORCHESTRATOR",
            action="RUN_COMPLETED",
            target=run_id,
            run_id=run_id,
            result="COMPLETED",
            details={"completed_at": now.isoformat()},
        )

        pub = get_event_publisher()
        if pub:
            pub.publish(
                event_type=SystemEventType.RUN_COMPLETED,
                payload={"run_id": run_id, "status": "COMPLETED", "completed_at": now.isoformat()},
                run_id=run_id,
                session=session,
            )

    logger.info(f"WorkflowRun '{run_id}' đã hoàn thành 100% thành công!")
    return {
        "is_completed": True,
        "current_stage": "completed",
        "next_node": "end",
    }
