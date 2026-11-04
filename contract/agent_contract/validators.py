import re
from pathlib import Path
from typing import Any

from agent_contract.enums import AgentRole, TaskStatus
from agent_contract.registry import CAPABILITY_REGISTRY

AGENT_ID_REGEX = r"^[a-z_]+-\d{2}$"
_AGENT_ID_PATTERN = re.compile(AGENT_ID_REGEX)

# Các URI scheme được hỗ trợ mở rộng ở System Phase
_GITHUB_PULL_PATTERN = re.compile(r"^github://([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)/pull/(\d+)$")
_GITHUB_COMMIT_PATTERN = re.compile(r"^github://([a-zA-Z0-9_.-]+)/([a-zA-Z0-9_.-]+)/commit/([a-fA-F0-9]{7,40})$")
_ARTIFACT_STORE_PATTERN = re.compile(r"^artifact://([a-zA-Z0-9_.-]+)$")


def validate_agent_id(agent_id: str) -> str:
    """Xác thực agent_id theo định dạng <role viết thường>-NN với role thuộc AgentRole (ví dụ: pm-01, devops-01)."""
    if not isinstance(agent_id, str) or not _AGENT_ID_PATTERN.match(agent_id):
        raise ValueError(
            f"agent_id '{agent_id}' không hợp lệ. Phải khớp định dạng '{AGENT_ID_REGEX}' (ví dụ: pm-01, devops-01)."
        )
    prefix = agent_id.rsplit("-", 1)[0]
    valid_prefixes = {role.value.lower() for role in AgentRole}
    if prefix not in valid_prefixes:
        raise ValueError(
            f"agent_id '{agent_id}' chứa tiền tố role '{prefix}' không hợp lệ. "
            f"Role phải thuộc AgentRole: {sorted(valid_prefixes)}"
        )
    return agent_id


def validate_artifact_uri(uri: str) -> str:
    """Xác thực uri của artifact:

    Cho phép đường dẫn tương đối (không '..', không tuyệt đối),
    hoặc đúng ba dạng URI đặc biệt:
    1. github://owner/repo/pull/<n>
    2. github://owner/repo/commit/<sha>
    3. artifact://<id>
    """
    if not isinstance(uri, str) or not uri.strip():
        raise ValueError("uri của artifact không được để trống.")

    normalized_uri = uri.replace("\\", "/")

    # Chặn path traversal '..' trong mọi trường hợp
    parts = normalized_uri.split("/")
    if ".." in parts:
        raise ValueError(f"uri '{uri}' không được chứa '..' để ngăn ngừa path traversal.")

    # Kiểm tra các URI scheme mở rộng cho System Phase
    if uri.startswith("github://"):
        if _GITHUB_PULL_PATTERN.match(uri) or _GITHUB_COMMIT_PATTERN.match(uri):
            return uri
        raise ValueError(
            f"uri GitHub '{uri}' không hợp lệ. Chỉ cho phép đúng 2 dạng: "
            "github://owner/repo/pull/<n> hoặc github://owner/repo/commit/<sha>."
        )

    if uri.startswith("artifact://"):
        if _ARTIFACT_STORE_PATTERN.match(uri):
            return uri
        raise ValueError(
            f"uri Artifact Store '{uri}' không hợp lệ. Phải khớp định dạng: artifact://<id>."
        )

    # Chặn các URI scheme ngoài quy định (http://, ftp://, file://...)
    if "://" in uri:
        raise ValueError(
            f"Scheme URI trong '{uri}' không được hỗ trợ. "
            "Chỉ cho phép github://, artifact:// hoặc đường dẫn tương đối."
        )

    # Đối với đường dẫn tương đối: Chặn đường dẫn tuyệt đối (Unix '/' hoặc Windows 'C:')
    if normalized_uri.startswith("/") or re.match(r"^[a-zA-Z]:", uri) or Path(uri).is_absolute():
        raise ValueError(f"uri '{uri}' phải là đường dẫn tương đối, không được là đường dẫn tuyệt đối.")

    return uri


def validate_capabilities(role: AgentRole, capabilities: list[str]) -> list[str]:
    """Xác thực danh sách capabilities phải là tập con của CAPABILITY_REGISTRY[role]."""
    allowed_caps = set(CAPABILITY_REGISTRY.get(role, []))
    invalid_caps = [cap for cap in capabilities if cap not in allowed_caps]
    if invalid_caps:
        raise ValueError(
            f"Role '{role.value}' chứa capability không hợp lệ: {invalid_caps}. "
            f"Chỉ được phép trong: {sorted(allowed_caps)}"
        )
    return capabilities


def validate_task_result_status_and_error(status: TaskStatus, error: Any | None) -> None:
    """Xác thực ràng buộc giữa status và error trong TaskResult:

    - status == COMPLETED -> error phải là None.
    - status in (FAILED, BLOCKED) -> error bắt buộc phải có.
    - status == CANCELLED -> error là tùy chọn (None hoặc AgentError).
    """
    if status == TaskStatus.COMPLETED and error is not None:
        raise ValueError("Khi status là COMPLETED, trường 'error' bắt buộc phải là None.")

    if status in (TaskStatus.FAILED, TaskStatus.BLOCKED) and error is None:
        raise ValueError(f"Khi status là {status.value}, trường 'error' bắt buộc phải có thông tin AgentError.")
