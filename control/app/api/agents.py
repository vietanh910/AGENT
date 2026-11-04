"""Endpoint danh sách và trạng thái Agent (GET /api/v1/agents)."""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from control.app.api.dependencies import get_db, require_authenticated
from control.app.security.authentication import Principal
from control.app.services.registry import RegistryService

router = APIRouter(prefix="/agents", tags=["Agents"])


@router.get("", response_model=List[Dict[str, Any]])
def list_agents(
    session: Session = Depends(get_db),
    principal: Principal = Depends(require_authenticated),
) -> List[Dict[str, Any]]:
    """Trả về bảng 10 agent: agent_id, role, machine, trạng thái, last_seen, current_task."""
    service = RegistryService(session)
    # Đảm bảo registry mặc định đã có dữ liệu
    service.init_defaults()
    session.commit()
    return service.get_agents_overview()
