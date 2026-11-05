"""Background driver. Durable work lives in MySQL, never in this process's memory."""

import asyncio
import logging

from sqlalchemy import select

from control.app.config import settings
from control.app.database.models import WorkflowRunModel
from control.app.database.session import get_session_factory
from control.app.integrations.linear.client import FakeWorkTracker, LinearClient
from control.app.integrations.linear.config_manager import initialize_and_validate_linear_schema, load_linear_config
from control.app.integrations.linear.polling import LinearPollingService
from control.app.integrations.linear.sync import LinearSyncService
from control.app.services.dispatcher import DispatcherService
from control.app.services.project_coordinator import ProjectCoordinator

logger = logging.getLogger(__name__)


async def run_project_loop(stop_event, redis_client, publisher):
    factory = get_session_factory()
    linear = None
    polling = None
    planning_tasks = {}
    dispatcher = DispatcherService(session_factory=factory, redis_client=redis_client, publisher=publisher)
    while not stop_event.is_set():
        try:
            for run_id, task in list(planning_tasks.items()):
                if task.done():
                    try:
                        task.result()
                    except Exception:
                        logger.exception("Background planning interrupted for %s; retained in DB for recovery", run_id)
                    del planning_tasks[run_id]
            if linear is None:
                def initialize():
                    tracker = FakeWorkTracker() if settings.LINEAR_DRY_RUN else LinearClient(settings.LINEAR_API_KEY)
                    cfg = load_linear_config()
                    team = settings.LINEAR_TEAM_ID
                    if "placeholder" in team:
                        team = cfg["team_id"]
                    cache = initialize_and_validate_linear_schema(tracker, team, cfg)
                    return (LinearSyncService(tracker, cache, redis_client, factory),
                            LinearPollingService(tracker, cache, factory))
                linear, polling = await asyncio.to_thread(initialize)
            coordinator = ProjectCoordinator(factory, dispatcher, linear)
            def pending_plans():
                with factory() as session:
                    return [r.run_id for r in session.scalars(select(WorkflowRunModel).where(
                        WorkflowRunModel.status == "PLANNING")) if (r.run_metadata or {}).get("run_kind") == "change"]
            from control.app.services.change_requests import plan_change_in_background
            for run_id in await asyncio.to_thread(pending_plans):
                if run_id not in planning_tasks or planning_tasks[run_id].done():
                    planning_tasks[run_id] = asyncio.create_task(asyncio.to_thread(plan_change_in_background, run_id))

            def cycle():
                with factory() as session:
                    runs = list(session.scalars(select(WorkflowRunModel).where(
                        WorkflowRunModel.status.in_(["RUNNING", "AWAITING_APPROVAL", *(["BLOCKED"] if settings.PM_AUTO_RECOVERY else [])]))))
                    ids = [r.run_id for r in runs if (r.plan or {}).get("execution_mode") == "modules"]
                for run_id in ids:
                    try:
                        coordinator.tick(run_id)
                    except Exception:
                        logger.exception("Project cycle failed for %s; state retained for retry", run_id)
                linear.process_queue_worker(max_items=8)
                polling.poll_once()
            await asyncio.to_thread(cycle)
        except Exception:
            logger.exception("Project coordinator unavailable; retrying without dispatch")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=max(settings.PROJECT_POLL_SECONDS, 0.2))
        except asyncio.TimeoutError:
            pass
