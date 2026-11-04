"""edges.py - Định nghĩa các cạnh chuyển tiếp có điều kiện (Conditional Edges) cho LangGraph (Prompt S9)."""

from __future__ import annotations

import logging
from typing import Literal

from langgraph.graph import END

from control.app.orchestrator.state import WorkflowState

logger = logging.getLogger(__name__)


def route_after_await_approval(
    state: WorkflowState,
) -> Literal["wait_for_event", "schedule", "handle_failure", "__end__"]:
    """Định tuyến sau khi kiểm tra trạng thái phê duyệt kế hoạch."""
    target = state.get("next_node")
    if target == "handle_failure" or state.get("stop_requested"):
        return "handle_failure"
    if target == "end" or state.get("current_stage") == "awaiting_plan_approval":
        return END
    if target == "schedule":
        return "schedule"
    return "wait_for_event"


def route_after_wait_for_event(
    state: WorkflowState,
) -> Literal["process_result", "schedule", "handle_failure", "__end__"]:
    """Định tuyến sau khi kiểm tra sự kiện: nếu có sự kiện thì xử lý, không có thì sang schedule."""
    target = state.get("next_node")
    if target == "process_result":
        return "process_result"
    if target == "handle_failure":
        return "handle_failure"
    if target == "end":
        return END
    return "schedule"


def route_after_process_result(
    state: WorkflowState,
) -> Literal["schedule", "fix_loop", "retry_task", "gate", "devops_deploy", "health_check", "finalize", "handle_failure", "__end__"]:
    """Định tuyến sau khi xử lý kết quả task vừa nộp về."""
    target = state.get("next_node", "schedule")
    if target == "fix_loop":
        return "fix_loop"
    if target == "gate":
        return "gate"
    if target == "finalize":
        return "finalize"
    if target == "handle_failure":
        return "handle_failure"
    if target == "retry_task":
        return "retry_task"
    if target == "devops_deploy":
        return "devops_deploy"
    if target == "health_check":
        return "health_check"
    if target == "end":
        return END
    return "schedule"


def route_after_fix_loop(
    state: WorkflowState,
) -> Literal["schedule", "handle_failure"]:
    """Định tuyến sau khi xử lý vòng sửa lỗi (fix_loop)."""
    if state.get("is_blocked") or state.get("next_node") == "handle_failure":
        return "handle_failure"
    return "schedule"


def route_after_gate(
    state: WorkflowState,
) -> Literal["schedule", "handle_failure", "__end__"]:
    """Định tuyến sau khi kiểm tra cổng kiểm soát (gate)."""
    target = state.get("next_node")
    if target == "schedule":
        return "schedule"
    if target == "handle_failure":
        return "handle_failure"
    return END


def route_after_schedule(
    state: WorkflowState,
) -> Literal["dispatch_ready", "gate", "finalize", "handle_failure", "__end__"]:
    """Định tuyến sau khi lập lịch các task READY."""
    if state.get("is_completed") or state.get("next_node") == "finalize":
        return "finalize"
    if state.get("is_blocked") or state.get("next_node") == "handle_failure":
        return "handle_failure"
    if state.get("next_node") == "gate":
        return "gate"
    if state.get("ready_tasks_count", 0) > 0:
        return "dispatch_ready"
    return END
