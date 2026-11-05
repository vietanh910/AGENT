import socket
from pathlib import Path
from typing import Any
from pydantic import ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent_contract import CONTRACT_VERSION
from agent_contract.enums import AgentRole

# Thư mục gốc của agent (nơi chứa file config.py)
AGENT_DIR = Path(__file__).resolve().parent


def is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Kiểm tra xem một cổng mạng (port) đã bị chiếm dụng trên host hay chưa."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            target_host = "127.0.0.1" if host in ["0.0.0.0", ""] else host
            if s.connect_ex((target_host, port)) == 0:
                return True
        except Exception:
            pass

    for h in ([host, "127.0.0.1"] if host in ["0.0.0.0", ""] else [host]):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                bind_h = "" if h == "0.0.0.0" else h
                s.bind((bind_h, port))
            except OSError:
                return True
    return False


def find_available_port(start_port: int, host: str = "127.0.0.1") -> int:
    """Tìm một cổng mạng khả dụng kế tiếp bắt đầu từ start_port."""
    for p in range(start_port + 1, start_port + 100):
        if not is_port_in_use(p, host):
            return p
    return start_port + 1


def parse_semver(version_str: str) -> tuple[int, ...]:
    """Phân tích chuỗi phiên bản semver thành tuple số nguyên để so sánh thứ tự."""
    try:
        clean = version_str.strip().lstrip("v").split("-")[0]
        return tuple(int(x) for x in clean.split("."))
    except Exception:
        return (0, 0, 0)


class PMConfig(BaseSettings):
    """Cấu hình cho PM Agent (pm-01)."""

    model_config = SettingsConfigDict(
        env_file=str(AGENT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    base_dir: Path = AGENT_DIR

    host: str = "0.0.0.0"
    port: int | None = None
    contract_min_version: str = "1.0.0"
    agent_api_key: str = ""
    max_upload_mb: int = 50

    # Cấu hình Conductor / Tự động hóa dự án (v1.1.0)
    pm_auto_approve: bool = False
    pm_project_mode: str = "managed"
    control_url: str = "http://127.0.0.1:8000"
    control_admin_token: str = ""
    pm_allow_real_deploy: bool = False
    max_fix_loops: int = 3
    max_tasks_per_project: int = 60
    project_timeout_minutes: int = 240

    agent_id: str = "pm-01"
    agent_role: AgentRole = AgentRole.PM
    llm_provider: str = "groq"
    llm_model: str = "llama-3.3-70b-versatile"
    llm_api_key: str = ""
    llm_base_url: str | None = None
    llm_temperature: float = 0.2
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2

    artifact_dir: str = "./artifacts"
    workspace_dir: str = "./workspace"
    data_dir: str = "./data"
    allowed_commands: str = ""
    dry_run: bool = True

    @field_validator("port", mode="before")
    @classmethod
    def validate_port_required(cls, v: Any) -> int:
        if v is None or (isinstance(v, str) and not v.strip()):
            raise ValueError("PORT là bắt buộc, không có giá trị mặc định chung.")
        try:
            val = int(v)
        except (ValueError, TypeError):
            raise ValueError(f"PORT phải là số nguyên hợp lệ, nhận được: {v}")
        if not (1 <= val <= 65535):
            raise ValueError(f"PORT phải nằm trong khoảng 1-65535, nhận được: {val}")
        return val

    @field_validator("artifact_dir", "workspace_dir", "data_dir", mode="before")
    @classmethod
    def resolve_agent_path(cls, v: Any, info: ValidationInfo) -> str:
        if not v:
            v = "."
        raw_p = Path(v)
        base = info.data.get("base_dir", AGENT_DIR) if info.data else AGENT_DIR
        if not isinstance(base, Path):
            base = Path(base).resolve()

        if not raw_p.is_absolute():
            resolved = (base / raw_p).resolve()
        else:
            resolved = raw_p.resolve()

        try:
            resolved.relative_to(base)
        except ValueError:
            raise ValueError(
                f"Đường dẫn '{v}' không hợp lệ: trỏ ra ngoài thư mục agent ({base}). Chỉ chấp nhận đường dẫn bên trong thư mục agent."
            )
        return str(resolved)

    def get_artifact_path(self) -> Path:
        p = Path(self.artifact_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_workspace_path(self) -> Path:
        p = Path(self.workspace_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def get_data_path(self) -> Path:
        p = Path(self.data_dir).resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p

    def verify_startup(self, check_port: bool = True) -> None:
        installed_ver = parse_semver(CONTRACT_VERSION)
        min_ver = parse_semver(self.contract_min_version)
        if installed_ver < min_ver:
            raise RuntimeError(
                f"Phiên bản agent_contract ({CONTRACT_VERSION}) thấp hơn yêu cầu tối thiểu ({self.contract_min_version}). Vui lòng nâng cấp agent_contract."
            )

        # Kiểm tra bảo mật LAN: nếu HOST khác 127.0.0.1 và localhost thì bắt buộc có AGENT_API_KEY
        host_str = (self.host or "").strip().lower()
        if host_str not in ["127.0.0.1", "localhost"] and not (self.agent_api_key and self.agent_api_key.strip()):
            raise RuntimeError(
                f"Bảo mật: HOST đang cấu hình là '{self.host}' (mở ra mạng ngoài). "
                f"Bắt buộc phải cấu hình AGENT_API_KEY để bảo vệ an toàn qua mạng LAN."
            )

        if check_port and is_port_in_use(self.port, self.host):
            suggested = find_available_port(self.port, self.host)
            raise RuntimeError(
                f"Cổng PORT {self.port} đã bị chiếm dụng trên host '{self.host}'. Gợi ý cổng khả dụng khác: {suggested}."
            )
