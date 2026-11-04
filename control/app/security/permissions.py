"""Phân quyền và ma trận kiểm soát truy cập cho Control Node PC1.

Ma trận truy cập:
- ADMIN: Tạo/điều khiển Run, duyệt/hủy/tạm dừng Gate, unblock task.
- WORKER: Đăng ký Worker, gửi Heartbeat, nộp kết quả TaskResult (chỉ cho agent thuộc máy mình).
- ĐỌC CHUNG: Tra cứu Run, Tasks, Agents.
"""

from __future__ import annotations

from typing import Optional, Union

from agent_contract.enums import AgentRole, ErrorCode
from agent_contract.models import AgentError
from agent_contract.registry import ACTION_MAP
from control.app.security.authentication import AdminPrincipal, Principal, WorkerPrincipal


class SecurityException(Exception):
    """Lỗi bảo mật cơ sở."""

    def __init__(self, status_code: int, code: ErrorCode, message: str, details: Optional[dict] = None) -> None:
        self.status_code = status_code
        self.agent_error = AgentError(
            code=code,
            message=message,
            details=details or {},
            retryable=False,
        )
        super().__init__(message)


class AuthenticationFailedException(SecurityException):
    """401 Unauthorized - Thiếu hoặc sai Bearer token."""

    def __init__(self, message: str = "Bearer token không hợp lệ hoặc đã hết hạn.", details: Optional[dict] = None) -> None:
        super().__init__(
            status_code=401,
            code=ErrorCode.PERMISSION_DENIED,
            message=message,
            details=details,
        )


class PermissionDeniedException(SecurityException):
    """403 Forbidden - Không có quyền thực hiện thao tác."""

    def __init__(self, message: str = "Không có quyền thực hiện hành động này.", details: Optional[dict] = None) -> None:
        super().__init__(
            status_code=403,
            code=ErrorCode.PERMISSION_DENIED,
            message=message,
            details=details,
        )


def enforce_admin(principal: Optional[Principal]) -> AdminPrincipal:
    """Yêu cầu quyền ADMIN."""
    if principal is None:
        raise AuthenticationFailedException()
    if not isinstance(principal, AdminPrincipal):
        raise PermissionDeniedException("Hành động này chỉ dành riêng cho Admin.")
    return principal


def enforce_worker(
    principal: Optional[Principal],
    target_worker_id: Optional[str] = None,
) -> WorkerPrincipal:
    """Yêu cầu quyền WORKER và kiểm tra worker_id trùng khớp."""
    if principal is None:
        raise AuthenticationFailedException()
    if not isinstance(principal, WorkerPrincipal):
        raise PermissionDeniedException("Hành động này chỉ dành cho Worker Host.")

    if target_worker_id and principal.worker_id != target_worker_id:
        raise PermissionDeniedException(
            f"Worker '{principal.worker_id}' không được phép thao tác thay cho '{target_worker_id}'."
        )
    return principal


def enforce_worker_agent_ownership(
    principal: Optional[Principal],
    agent_id: str,
    task_id: str,
) -> WorkerPrincipal:
    """Kiểm tra nghiêm ngặt: Worker chỉ được gửi kết quả của task đã giao cho agent của chính nó."""
    worker = enforce_worker(principal)

    if not worker.can_operate_agent(agent_id):
        raise PermissionDeniedException(
            f"Worker '{worker.worker_id}' không quản lý agent '{agent_id}' để báo cáo kết quả cho task '{task_id}'. "
            f"Các agent được phép: {worker.allowed_agents}"
        )
    return worker


class ActionPolicy:
    """Chính sách phân quyền hành động theo vai trò (Role -> Action) theo Agent Contract."""

    @staticmethod
    def is_action_allowed(role: Union[AgentRole, str], action: str) -> bool:
        """Kiểm tra role có quyền thực hiện action này không."""
        if not role or not action:
            return False

        role_enum: Optional[AgentRole] = None
        if isinstance(role, AgentRole):
            role_enum = role
        else:
            role_str = str(role).strip().upper()
            if role_str == "GATE":
                return action in ("approve_scope", "approve_prod")
            try:
                role_enum = AgentRole(role_str)
            except ValueError:
                return False

        allowed_actions = ACTION_MAP.get(role_enum, {})
        return action in allowed_actions

    @classmethod
    def enforce_action(cls, role: Union[AgentRole, str], action: str) -> None:
        """Bắt buộc hành động phải được phép, ném PermissionDeniedException nếu vi phạm."""
        if not cls.is_action_allowed(role, action):
            role_name = role.value if isinstance(role, AgentRole) else str(role)
            raise PermissionDeniedException(
                f"Role '{role_name}' không có quyền thực hiện action '{action}'. Action không thuộc ACTION_MAP.",
                details={"role": role_name, "action": action},
            )
