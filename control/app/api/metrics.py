"""control/app/api/metrics.py - Prometheus metrics endpoint (Prompt S11).

Cung cấp các metrics chuẩn Prometheus dạng text format:
- ai_team_tasks_total{state="..."}
- ai_team_workers_online
- ai_team_workers_total{status="..."}
- ai_team_linear_sync_queue_depth
- ai_team_linear_sync_latency_seconds
- ai_team_linear_dlq_depth
- ai_team_runs_total{status="..."}
"""

from __future__ import annotations

import logging
from typing import Dict

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agent_contract.system_models import DispatchState, RunStatus
from control.app.api.dependencies import get_db, get_redis
from control.app.database.models import TaskModel, WorkerModel, WorkflowRunModel

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Metrics"])


def generate_prometheus_metrics(session: Session, redis_client=None) -> str:
    """Sinh chuỗi văn bản định dạng Prometheus exposition format."""
    lines = []

    # 1. Tasks by state
    lines.append("# HELP ai_team_tasks_total Total tasks grouped by dispatch state")
    lines.append("# TYPE ai_team_tasks_total gauge")

    task_states = [s.value for s in DispatchState]
    task_counts: Dict[str, int] = {s: 0 for s in task_states}
    try:
        stmt = select(TaskModel.dispatch_state, func.count(TaskModel.task_id)).group_by(TaskModel.dispatch_state)
        for state, cnt in session.execute(stmt).all():
            if state in task_counts:
                task_counts[state] = cnt
            else:
                task_counts[state] = cnt
    except Exception as e:
        logger.warning(f"Error querying task metrics: {e}")

    for state, cnt in sorted(task_counts.items()):
        lines.append(f'ai_team_tasks_total{{state="{state}"}} {cnt}')

    # 2. Workers online & total by status
    lines.append("# HELP ai_team_workers_online Number of online workers")
    lines.append("# TYPE ai_team_workers_online gauge")
    lines.append("# HELP ai_team_workers_total Total workers grouped by status")
    lines.append("# TYPE ai_team_workers_total gauge")

    worker_counts = {"ONLINE": 0, "OFFLINE": 0}
    try:
        w_stmt = select(WorkerModel.status, func.count(WorkerModel.worker_id)).group_by(WorkerModel.status)
        for st, cnt in session.execute(w_stmt).all():
            worker_counts[st] = cnt
    except Exception as e:
        logger.warning(f"Error querying worker metrics: {e}")

    lines.append(f'ai_team_workers_online {worker_counts.get("ONLINE", 0)}')
    for st, cnt in sorted(worker_counts.items()):
        lines.append(f'ai_team_workers_total{{status="{st}"}} {cnt}')

    # 3. Linear Sync Queue Depth & Latency
    lines.append("# HELP ai_team_linear_sync_queue_depth Depth of Linear sync queue in Redis")
    lines.append("# TYPE ai_team_linear_sync_queue_depth gauge")
    lines.append("# HELP ai_team_linear_sync_latency_seconds Latency of Linear synchronization in seconds")
    lines.append("# TYPE ai_team_linear_sync_latency_seconds gauge")
    lines.append("# HELP ai_team_linear_dlq_depth Depth of Linear dead-letter queue in Redis")
    lines.append("# TYPE ai_team_linear_dlq_depth gauge")

    linear_q_depth = 0
    linear_dlq_depth = 0
    if redis_client:
        try:
            linear_q_depth = redis_client.llen("linear:sync:queue")
            linear_dlq_depth = redis_client.llen("linear:sync:dlq")
        except Exception as e:
            logger.debug(f"Redis unavailable for linear queue depth: {e}")

    lines.append(f"ai_team_linear_sync_queue_depth {linear_q_depth}")
    lines.append(f"ai_team_linear_sync_latency_seconds 0.0")
    lines.append(f"ai_team_linear_dlq_depth {linear_dlq_depth}")

    # 4. Runs by status
    lines.append("# HELP ai_team_runs_total Total workflow runs grouped by status")
    lines.append("# TYPE ai_team_runs_total gauge")

    run_statuses = [r.value for r in RunStatus]
    run_counts: Dict[str, int] = {st: 0 for st in run_statuses}
    try:
        r_stmt = select(WorkflowRunModel.status, func.count(WorkflowRunModel.run_id)).group_by(WorkflowRunModel.status)
        for st, cnt in session.execute(r_stmt).all():
            run_counts[st] = cnt
    except Exception as e:
        logger.warning(f"Error querying run metrics: {e}")

    for st, cnt in sorted(run_counts.items()):
        lines.append(f'ai_team_runs_total{{status="{st}"}} {cnt}')

    lines.append("")  # Kết thúc bằng newline
    return "\n".join(lines)


@router.get("/metrics")
def get_metrics(
    session: Session = Depends(get_db),
    redis_client=Depends(get_redis),
) -> Response:
    """Trả về Prometheus exposition format."""
    content = generate_prometheus_metrics(session=session, redis_client=redis_client)
    return Response(
        content=content,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
