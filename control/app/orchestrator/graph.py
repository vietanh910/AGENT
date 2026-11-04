"""graph.py - Xây dựng và liên kết đồ thị LangGraph StateGraph (Prompt S9).

Nguyên tắc:
- Ghép nối toàn bộ 16 node và các cạnh chuyển tiếp có điều kiện tất định.
- Hỗ trợ truyền Checkpointer tùy chọn (bọc sau CheckpointStore).
- Khả năng khôi phục hoàn toàn từ bảng MySQL kể cả khi không dùng checkpointer.
"""

from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from control.app.orchestrator.edges import (
    route_after_await_approval,
    route_after_fix_loop,
    route_after_gate,
    route_after_process_result,
    route_after_schedule,
    route_after_wait_for_event,
)
from control.app.orchestrator.nodes import (
    await_plan_approval_node,
    create_linear_project_node,
    devops_deploy_node,
    dispatch_ready_node,
    finalize_node,
    fix_loop_node,
    gate_node,
    handle_failure_node,
    health_check_node,
    pm_planning_node,
    process_result_node,
    receive_request_node,
    retry_task_node,
    route_node,
    schedule_node,
    wait_for_event_node,
)
from control.app.orchestrator.state import WorkflowState

logger = logging.getLogger(__name__)


def build_orchestrator_graph(
    checkpointer: Optional[BaseCheckpointSaver] = None,
):
    """Xây dựng và biên dịch LangGraph StateGraph cho Orchestrator trên PC1."""
    workflow = StateGraph(WorkflowState)

    # 1. Thêm toàn bộ 16 node
    workflow.add_node("receive_request", receive_request_node)
    workflow.add_node("pm_planning", pm_planning_node)
    workflow.add_node("create_linear_project", create_linear_project_node)
    workflow.add_node("await_plan_approval", await_plan_approval_node)
    workflow.add_node("wait_for_event", wait_for_event_node)
    workflow.add_node("process_result", process_result_node)
    workflow.add_node("route", route_node)
    workflow.add_node("schedule", schedule_node)
    workflow.add_node("dispatch_ready", dispatch_ready_node)
    workflow.add_node("retry_task", retry_task_node)
    workflow.add_node("handle_failure", handle_failure_node)
    workflow.add_node("fix_loop", fix_loop_node)
    workflow.add_node("gate", gate_node)
    workflow.add_node("devops_deploy", devops_deploy_node)
    workflow.add_node("health_check", health_check_node)
    workflow.add_node("finalize", finalize_node)

    # 2. Thêm các cạnh cố định
    workflow.add_edge(START, "receive_request")
    workflow.add_edge("receive_request", "pm_planning")
    workflow.add_edge("pm_planning", "create_linear_project")
    workflow.add_edge("create_linear_project", "await_plan_approval")

    # 3. Cạnh có điều kiện sau await_plan_approval
    workflow.add_conditional_edges(
        "await_plan_approval",
        route_after_await_approval,
        {
            "wait_for_event": "wait_for_event",
            "schedule": "schedule",
            "handle_failure": "handle_failure",
            END: END,
        },
    )

    # 4. Cạnh có điều kiện sau wait_for_event
    workflow.add_conditional_edges(
        "wait_for_event",
        route_after_wait_for_event,
        {
            "process_result": "process_result",
            "schedule": "schedule",
            "handle_failure": "handle_failure",
            END: END,
        },
    )

    workflow.add_edge("process_result", "route")

    # 5. Cạnh có điều kiện sau route
    workflow.add_conditional_edges(
        "route",
        route_after_process_result,
        {
            "schedule": "schedule",
            "fix_loop": "fix_loop",
            "retry_task": "retry_task",
            "gate": "gate",
            "devops_deploy": "devops_deploy",
            "health_check": "health_check",
            "finalize": "finalize",
            "handle_failure": "handle_failure",
            END: END,
        },
    )

    # 6. Cạnh có điều kiện sau fix_loop
    workflow.add_conditional_edges(
        "fix_loop",
        route_after_fix_loop,
        {
            "schedule": "schedule",
            "handle_failure": "handle_failure",
        },
    )

    # 7. Cạnh sau retry_task, devops_deploy, health_check
    workflow.add_edge("retry_task", "schedule")
    workflow.add_edge("devops_deploy", "schedule")
    workflow.add_edge("health_check", "schedule")

    # 8. Cạnh sau gate
    workflow.add_conditional_edges(
        "gate",
        route_after_gate,
        {
            "schedule": "schedule",
            "handle_failure": "handle_failure",
            END: END,
        },
    )

    # 9. Cạnh có điều kiện sau schedule
    workflow.add_conditional_edges(
        "schedule",
        route_after_schedule,
        {
            "dispatch_ready": "dispatch_ready",
            "gate": "gate",
            "finalize": "finalize",
            "handle_failure": "handle_failure",
            END: END,
        },
    )

    workflow.add_edge("dispatch_ready", END)

    # 10. Điểm kết thúc
    workflow.add_edge("handle_failure", END)
    workflow.add_edge("finalize", END)

    # Biên dịch đồ thị kèm Checkpointer nếu có
    compiled_graph = workflow.compile(checkpointer=checkpointer)
    logger.info("Đã biên dịch Orchestrator StateGraph thành công.")
    return compiled_graph
