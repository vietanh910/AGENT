"""Tập hợp toàn bộ API Router v1 của Control Node Gateway."""

from fastapi import APIRouter

from control.app.api.agents import router as agents_router
from control.app.api.artifacts import router as artifacts_router
from control.app.api.metrics import router as metrics_router
from control.app.api.runs import router as runs_router
from control.app.api.tasks import router as tasks_router
from control.app.api.webhooks import router as webhooks_router
from control.app.api.workers import router as workers_router
from control.app.api.projects import router as projects_router

api_v1_router = APIRouter(prefix="/api/v1")
api_v1_router.include_router(runs_router)
api_v1_router.include_router(projects_router)
api_v1_router.include_router(agents_router)
api_v1_router.include_router(workers_router)
api_v1_router.include_router(tasks_router)
api_v1_router.include_router(webhooks_router)
api_v1_router.include_router(artifacts_router)
api_v1_router.include_router(metrics_router)

__all__ = ["api_v1_router", "metrics_router"]
