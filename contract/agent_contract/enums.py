from enum import Enum


class AgentRole(str, Enum):
    """Vai trò của Agent trong đội ngũ phát triển phần mềm."""
    PM = "PM"
    SA = "SA"
    BA = "BA"
    PO = "PO"
    DEV = "DEV"
    TESTER = "TESTER"
    DEVOPS = "DEVOPS"
    DBA = "DBA"
    SCRUM_MASTER = "SCRUM_MASTER"
    SECURITY = "SECURITY"


class AgentStatus(str, Enum):
    """Trạng thái hoạt động của Agent."""
    OFFLINE = "OFFLINE"
    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class TaskStatus(str, Enum):
    """Trạng thái thực thi của Task."""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class Priority(str, Enum):
    """Mức độ ưu tiên của Task."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class ArtifactType(str, Enum):
    """Loại Artifact được tạo hoặc chuyển giao giữa các Agent."""
    REQUIREMENT = "REQUIREMENT"
    USER_STORY = "USER_STORY"
    ARCHITECTURE = "ARCHITECTURE"
    DATABASE_SCHEMA = "DATABASE_SCHEMA"
    SOURCE_CODE = "SOURCE_CODE"
    TEST_CASE = "TEST_CASE"
    TEST_REPORT = "TEST_REPORT"
    SECURITY_REPORT = "SECURITY_REPORT"
    DEPLOYMENT = "DEPLOYMENT"
    DOCUMENTATION = "DOCUMENTATION"
    PLAN = "PLAN"
    BACKLOG = "BACKLOG"
    OTHER = "OTHER"


class ErrorCode(str, Enum):
    """Mã định danh lỗi chuẩn trong hệ thống."""
    INVALID_INPUT = "INVALID_INPUT"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    LLM_ERROR = "LLM_ERROR"
    TOOL_ERROR = "TOOL_ERROR"
    TIMEOUT = "TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    ARTIFACT_ERROR = "ARTIFACT_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"
    # Bổ sung cho System Phase (PC1 Control Node & Worker Hosts)
    WORKER_OFFLINE = "WORKER_OFFLINE"
    DISPATCH_FAILED = "DISPATCH_FAILED"
    LINEAR_ERROR = "LINEAR_ERROR"
    GITHUB_ERROR = "GITHUB_ERROR"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
