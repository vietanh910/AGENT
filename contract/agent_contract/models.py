from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from agent_contract.enums import (
    AgentRole,
    AgentStatus,
    ArtifactType,
    ErrorCode,
    Priority,
    TaskStatus,
)
from agent_contract.validators import (
    validate_agent_id,
    validate_artifact_uri,
    validate_capabilities,
    validate_task_result_status_and_error,
)

CONTRACT_VERSION: str = "1.4.0"


class AgentIdentity(BaseModel):
    """Thông tin nhận diện và năng lực tự khai báo của Agent."""
    contract_version: str = CONTRACT_VERSION
    agent_id: str
    role: AgentRole
    name: str
    version: str
    capabilities: list[str]
    status: AgentStatus = AgentStatus.IDLE

    @field_validator("agent_id")
    @classmethod
    def check_agent_id(cls, v: str) -> str:
        return validate_agent_id(v)

    @model_validator(mode="after")
    def check_capabilities(self) -> "AgentIdentity":
        validate_capabilities(self.role, self.capabilities)
        return self


class AgentState(BaseModel):
    """Trạng thái hiện thời của Agent phục vụ giám sát và Dashboard."""
    agent_id: str
    role: AgentRole
    status: AgentStatus
    current_task: str | None = None
    updated_at: datetime

    @field_validator("agent_id")
    @classmethod
    def check_agent_id(cls, v: str) -> str:
        return validate_agent_id(v)


class ArtifactRef(BaseModel):
    """Tham chiếu siêu dữ liệu tới một Artifact trong hệ thống."""
    artifact_id: str
    name: str
    type: ArtifactType
    version: str = "1.0"
    uri: str
    created_by: str
    created_at: datetime
    checksum: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("uri")
    @classmethod
    def check_uri(cls, v: str) -> str:
        return validate_artifact_uri(v)

    @field_validator("created_by")
    @classmethod
    def check_created_by(cls, v: str) -> str:
        return validate_agent_id(v)


class Constraints(BaseModel):
    """Các ràng buộc về thời hạn, ưu tiên và timeout của tác vụ."""
    deadline: datetime | None = None
    priority: Priority = Priority.MEDIUM
    timeout_seconds: int = 1000


class TaskRequest(BaseModel):
    """Yêu cầu thực thi tác vụ gửi tới một Agent cụ thể."""
    contract_version: str = CONTRACT_VERSION
    task_id: str
    project_id: str
    agent_id: str
    role: AgentRole
    action: str
    instruction: str
    context: dict[str, Any] = Field(default_factory=dict)
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    constraints: Constraints = Field(default_factory=Constraints)

    @field_validator("agent_id")
    @classmethod
    def check_agent_id(cls, v: str) -> str:
        return validate_agent_id(v)


class AgentError(BaseModel):
    """Cấu trúc mô tả lỗi tiêu chuẩn trả về từ Agent."""
    code: ErrorCode
    message: str
    retryable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class NextAction(BaseModel):
    """Hành động đề xuất tiếp theo (gợi ý cho Orchestrator, không gọi trực tiếp)."""
    suggested_role: AgentRole
    action: str
    reason: str


class TaskResult(BaseModel):
    """Kết quả thực thi tác vụ trả về từ Agent."""
    contract_version: str = CONTRACT_VERSION
    task_id: str
    agent_id: str
    status: TaskStatus
    summary: str
    outputs: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)
    error: AgentError | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("agent_id")
    @classmethod
    def check_agent_id(cls, v: str) -> str:
        return validate_agent_id(v)

    @model_validator(mode="after")
    def check_status_and_error(self) -> "TaskResult":
        validate_task_result_status_and_error(self.status, self.error)
        return self


def make_error(
    code: ErrorCode,
    message: str,
    retryable: bool,
    **details: Any,
) -> AgentError:
    """Tạo đối tượng AgentError chuẩn từ mã lỗi, thông điệp và chi tiết bổ sung."""
    return AgentError(
        code=code,
        message=message,
        retryable=retryable,
        details=details,
    )


def failed_result(
    request: TaskRequest,
    error: AgentError,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TaskResult:
    """Tạo đối tượng TaskResult với trạng thái FAILED tương ứng với TaskRequest."""
    return TaskResult(
        contract_version=request.contract_version,
        task_id=request.task_id,
        agent_id=request.agent_id,
        status=TaskStatus.FAILED,
        summary=summary or error.message,
        outputs=[],
        artifacts=[],
        next_actions=[],
        error=error,
        metadata=metadata or {},
    )


def blocked_result(
    request: TaskRequest,
    error: AgentError,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TaskResult:
    """Tạo đối tượng TaskResult với trạng thái BLOCKED tương ứng với TaskRequest."""
    return TaskResult(
        contract_version=request.contract_version,
        task_id=request.task_id,
        agent_id=request.agent_id,
        status=TaskStatus.BLOCKED,
        summary=summary or error.message,
        outputs=[],
        artifacts=[],
        next_actions=[],
        error=error,
        metadata=metadata or {},
    )


# =============================================================================
# CÁC MODEL VERDICT (Dùng cho outputs[0] của TESTER, SECURITY, PO accept_release)
# =============================================================================

class VerdictStatus(str, Enum):
    """Trạng thái phán quyết nghiệm thu / kiểm thử."""
    PASS = "PASS"
    FAIL = "FAIL"


class FindingSeverity(str, Enum):
    """Mức độ nghiêm trọng của vấn đề / lỗi phát hiện."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class VerdictFinding(BaseModel):
    """Chi tiết từng phát hiện trong báo cáo phán quyết."""
    severity: FindingSeverity
    title: str
    detail: str


class Verdict(BaseModel):
    """Cấu trúc Verdict chuẩn bắt buộc trong outputs[0] của TESTER, SECURITY và PO."""
    verdict: VerdictStatus
    findings: list[VerdictFinding] = Field(default_factory=list)

