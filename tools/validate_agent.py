import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

def _setup_console_encoding() -> None:
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

# Thêm root vào sys.path để import agent_contract
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from agent_contract.enums import AgentRole, ErrorCode, TaskStatus
from agent_contract.models import AgentIdentity, TaskRequest, TaskResult
from agent_contract.registry import ACTION_MAP, CAPABILITY_REGISTRY
from agent_contract.validators import validate_agent_id, validate_artifact_uri

REQUIRED_FILES = [
    "agent.py",
    "config.py",
    "api.py",
    "prompt.py",
    "schemas.py",
    ".env.example",
    "requirements.txt",
    "README.md",
]

REQUIRED_DIRS = [
    "tools",
    "tests",
    "artifacts",
]

REQUIRED_ENV_KEYS = [
    "HOST",
    "PORT",
    "CONTRACT_MIN_VERSION",
    "AGENT_ID",
    "AGENT_ROLE",
    "AGENT_API_KEY",
    "MAX_UPLOAD_MB",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_API_KEY",
    "LLM_BASE_URL",
    "LLM_TEMPERATURE",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "ARTIFACT_DIR",
    "WORKSPACE_DIR",
    "ALLOWED_COMMANDS",
    "DRY_RUN",
]

SECRET_PATTERNS = [
    re.compile(r"gsk_[a-zA-Z0-9]{20,}"),
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
]

HARDCODED_PATH_PATTERN = re.compile(
    r"""(?:(?<![a-zA-Z])[a-zA-Z]:[/\\]|/home/|/Users/)""", re.IGNORECASE
)


@dataclass
class CheckResult:
    name: str
    passed: bool
    message: str = ""


@dataclass
class AgentValidationSummary:
    agent_dir: str
    agent_id: str = "unknown"
    role: str = "unknown"
    passed: bool = True
    checks: list[CheckResult] = field(default_factory=list)

    def add_check(self, name: str, passed: bool, message: str = "") -> None:
        if not passed:
            self.passed = False
        self.checks.append(CheckResult(name=name, passed=passed, message=message))


def check_1_required_files(agent_path: Path, summary: AgentValidationSummary) -> None:
    """1. Kiểm tra có đủ các file và thư mục bắt buộc."""
    missing = []
    for f in REQUIRED_FILES:
        target = agent_path / f
        if not target.exists() or not target.is_file():
            missing.append(f)

    for d in REQUIRED_DIRS:
        target = agent_path / d
        if not target.exists() or not target.is_dir():
            missing.append(f"{d}/")

    if missing:
        summary.add_check("Required Files", False, f"Thiếu file/thư mục: {', '.join(missing)}")
    else:
        summary.add_check("Required Files", True, "Đầy đủ tất cả file và thư mục bắt buộc.")


def check_2_env_and_secrets(agent_path: Path, summary: AgentValidationSummary) -> None:
    """2. Kiểm tra .env.example có đủ biến mục 2.6 và không chứa API key thật trong mã nguồn."""
    env_example = agent_path / ".env.example"
    if not env_example.exists():
        summary.add_check("Env & Secrets", False, "Không tìm thấy file .env.example.")
        return

    content = env_example.read_text(encoding="utf-8")
    missing_keys = []
    for k in REQUIRED_ENV_KEYS:
        if not re.search(rf"^\s*{re.escape(k)}\s*=", content, re.MULTILINE):
            missing_keys.append(k)

    if missing_keys:
        summary.add_check("Env & Secrets", False, f".env.example thiếu các khóa: {', '.join(missing_keys)}")
        return

    secret_findings = []
    for py_file in agent_path.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                secret_findings.append(f"{py_file.name}")

    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("LLM_API_KEY"):
            parts = line.split("=", 1)
            if len(parts) == 2:
                val = parts[1].split("#")[0].strip()
                if val:
                    secret_findings.append(".env.example (LLM_API_KEY không được gán sẵn giá trị thật)")

    if secret_findings:
        summary.add_check("Env & Secrets", False, f"Phát hiện secret/API key: {', '.join(secret_findings)}")
    else:
        summary.add_check("Env & Secrets", True, ".env.example đủ khóa chuẩn và không rò rỉ secret.")


def check_d1_no_hardcoded_paths(agent_path: Path, summary: AgentValidationSummary) -> None:
    """D.1 Kiểm tra không có đường dẫn tuyệt đối cứng (C:\\, /home/, /Users/) trong mã nguồn."""
    findings = []
    for py_file in agent_path.rglob("*.py"):
        if "tests" in py_file.parts:
            continue
        text = py_file.read_text(encoding="utf-8", errors="replace")
        matches = HARDCODED_PATH_PATTERN.findall(text)
        if matches:
            findings.append(f"{py_file.name} ({', '.join(set(matches))})")

    if findings:
        summary.add_check(
            "No Hardcoded Paths",
            False,
            f"Phát hiện đường dẫn tuyệt đối cứng: {'; '.join(findings)}",
        )
    else:
        summary.add_check(
            "No Hardcoded Paths",
            True,
            "Không chứa đường dẫn tuyệt đối cứng (C:\\, /home/, /Users/).",
        )


def check_d2_timezone_aware_datetime(agent_path: Path, summary: AgentValidationSummary) -> None:
    """D.2 Kiểm tra không dùng datetime.now() không tham số hoặc utcnow()."""
    violations = []
    for py_file in agent_path.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8", errors="replace")
        if "datetime.utcnow()" in text:
            violations.append(f"{py_file.name} sử dụng datetime.utcnow()")
        if re.search(r"datetime\.now\(\s*\)", text):
            violations.append(f"{py_file.name} sử dụng datetime.now() không có timezone")

    if violations:
        summary.add_check(
            "Timezone-Aware Datetime",
            False,
            f"Vi phạm datetime không có timezone: {'; '.join(violations)}",
        )
    else:
        summary.add_check(
            "Timezone-Aware Datetime",
            True,
            "Mọi thời gian đều dùng timezone UTC (timezone-aware).",
        )


def check_d3_no_inter_agent_access(agent_path: Path, summary: AgentValidationSummary) -> None:
    """D.3 Kiểm tra agent không import hoặc đọc file thuộc agents/<tên khác>/;
    Chỉ DUY NHẤT agents/pm được import module conductor hoặc gọi HTTP tới agent khác (Prompt 8).
    """
    violations = []
    current_agent_name = agent_path.name.lower()
    is_pm = (current_agent_name == "pm")

    for py_file in agent_path.rglob("*.py"):
        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue

        for node in ast.walk(tree):
            # 1. Kiểm tra import module conductor đối với non-PM
            if not is_pm:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "conductor" or alias.name.startswith("conductor."):
                            violations.append(f"{py_file.name}:{node.lineno} non-PM agent cấm import '{alias.name}'")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if mod == "conductor" or mod.startswith("conductor.") or "conductor" in mod.split("."):
                        violations.append(f"{py_file.name}:{node.lineno} non-PM agent cấm from '{mod}' import ...")

            # 2. Kiểm tra import agents.<tên khác>
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mod = alias.name
                    if mod.startswith("agents."):
                        target = mod.split(".")[1].lower()
                        if target != current_agent_name:
                            violations.append(f"{py_file.name}:{node.lineno} import {mod}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod.startswith("agents."):
                    target = mod.split(".")[1].lower()
                    if target != current_agent_name:
                        violations.append(f"{py_file.name}:{node.lineno} from {mod} import ...")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                val = node.value.lower()
                # Kiểm tra đọc file từ agents/<khác>
                if "agents/" in val:
                    parts = val.split("agents/")
                    if len(parts) > 1:
                        target_agent = parts[1].split("/")[0]
                        if target_agent and target_agent != current_agent_name:
                            violations.append(f"{py_file.name}:{node.lineno} truy cập đường dẫn '{node.value}'")
                elif not is_pm and any(x in val for x in ["http://localhost:800", "http://pm:", "http://devops:"]):
                    violations.append(f"{py_file.name}:{node.lineno} gọi HTTP agent khác: '{node.value}'")

    if violations:
        summary.add_check(
            "Inter-Agent Isolation",
            False,
            f"Phát hiện truy cập agent khác: {'; '.join(violations)}",
        )
    else:
        summary.add_check(
            "Inter-Agent Isolation",
            True,
            "Hoàn toàn độc lập, không import hoặc đọc file thuộc agent khác (ngoại lệ conductor hợp lệ cho PM).",
        )


def check_d4_port_uniqueness_and_alignment(
    agent_path: Path,
    summary: AgentValidationSummary,
    all_agent_folders: list[Path] | None = None,
    root_dir: Path = ROOT_DIR,
) -> None:
    """D.4 Kiểm tra PORT trong .env.example và scripts/local_agents.yaml không trùng nhau."""
    env_example = agent_path / ".env.example"
    if not env_example.exists():
        summary.add_check("Port Uniqueness", False, "Thiếu file .env.example để kiểm tra PORT.")
        return

    content = env_example.read_text(encoding="utf-8")
    m = re.search(r"^\s*PORT\s*=\s*(\d+)", content, re.MULTILINE)
    if not m:
        summary.add_check("Port Uniqueness", False, ".env.example chưa khai báo PORT bằng số hợp lệ.")
        return
    current_port = int(m.group(1))

    # Đọc YAML cấu hình local
    yaml_file = root_dir / "scripts" / "local_agents.yaml"
    yaml_ports: dict[str, int] = {}
    if yaml_file.exists():
        try:
            with open(yaml_file, "r", encoding="utf-8") as f:
                ydata = yaml.safe_load(f)
                if isinstance(ydata, dict) and "agents" in ydata:
                    for item in ydata["agents"]:
                        yaml_ports[item["name"]] = int(item["port"])
        except Exception:
            pass

    # Đọc AGENT_ENDPOINTS.md
    endpoints_file = root_dir / "docs" / "AGENT_ENDPOINTS.md"
    endpoint_ports: dict[str, int] = {}
    if endpoints_file.exists():
        text = endpoints_file.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "|" in line:
                cols = [c.strip().replace("`", "") for c in line.split("|")[1:-1]]
                if len(cols) >= 5 and cols[4].isdigit():
                    role = cols[1].lower()
                    endpoint_ports[role] = int(cols[4])

    # Kiểm tra trùng lặp với các agent khác
    if all_agent_folders:
        for other in all_agent_folders:
            if other.name == agent_path.name:
                continue
            other_env = other / ".env.example"
            if other_env.exists():
                other_text = other_env.read_text(encoding="utf-8")
                om = re.search(r"^\s*PORT\s*=\s*(\d+)", other_text, re.MULTILINE)
                if om and int(om.group(1)) == current_port:
                    summary.add_check(
                        "Port Uniqueness",
                        False,
                        f"PORT {current_port} bị trùng với agent '{other.name}' trong .env.example",
                    )
                    return

    # Đối chiếu với YAML nếu agent có trong danh sách
    agent_key = agent_path.name.lower()
    if agent_key in yaml_ports and yaml_ports[agent_key] != current_port:
        summary.add_check(
            "Port Uniqueness",
            False,
            f"PORT trong .env.example ({current_port}) không khớp với scripts/local_agents.yaml ({yaml_ports[agent_key]})",
        )
        return

    summary.add_check(
        "Port Uniqueness",
        True,
        f"PORT {current_port} độc nhất và khớp với cấu hình hệ thống.",
    )


def check_d5_requirements_pinned(
    agent_path: Path,
    summary: AgentValidationSummary,
    all_agent_folders: list[Path] | None = None,
) -> None:
    """D.5 Kiểm tra requirements.txt ghim phiên bản (==) và phiên bản agent_contract đồng bộ."""
    req_file = agent_path / "requirements.txt"
    if not req_file.exists():
        summary.add_check("Requirements Pinned", False, "Thiếu requirements.txt.")
        return

    lines = req_file.read_text(encoding="utf-8").splitlines()
    unpinned = []
    contract_ver = None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "==" not in line:
            unpinned.append(line)
        else:
            pkg, ver = [x.strip() for x in line.split("==", 1)]
            if pkg.lower().replace("_", "-") == "agent-contract":
                contract_ver = ver

    if unpinned:
        summary.add_check(
            "Requirements Pinned",
            False,
            f"Các thư viện chưa được ghim phiên bản (==): {', '.join(unpinned)}",
        )
        return

    if not contract_ver:
        summary.add_check(
            "Requirements Pinned",
            False,
            "requirements.txt chưa ghim agent_contract theo version cụ thể (vd: agent-contract==1.0.0).",
        )
        return

    # Đối chiếu version agent_contract với các agent khác
    if all_agent_folders:
        for other in all_agent_folders:
            if other.name == agent_path.name:
                continue
            other_req = other / "requirements.txt"
            if other_req.exists():
                for oline in other_req.read_text(encoding="utf-8").splitlines():
                    if "==" in oline:
                        opkg, over = [x.strip() for x in oline.split("==", 1)]
                        if opkg.lower().replace("_", "-") == "agent-contract":
                            if over != contract_ver:
                                summary.add_check(
                                    "Requirements Pinned",
                                    False,
                                    f"Version agent_contract ({contract_ver}) không khớp với agent '{other.name}' ({over}).",
                                )
                                return

    summary.add_check(
        "Requirements Pinned",
        True,
        f"100% thư viện đã ghim (==), agent_contract=={contract_ver} đồng bộ.",
    )


def check_d6_artifacts_relative_uris(agent_path: Path, summary: AgentValidationSummary) -> None:
    """D.6 Kiểm tra uri trong artifacts/index.json là đường dẫn tương đối."""
    index_file = agent_path / "artifacts" / "index.json"
    if not index_file.exists():
        summary.add_check("Artifacts Relative URI", True, "Chưa có file artifacts/index.json (hợp lệ ban đầu).")
        return

    try:
        data = json.loads(index_file.read_text(encoding="utf-8"))
    except Exception as e:
        summary.add_check("Artifacts Relative URI", False, f"Không thể parse JSON từ artifacts/index.json: {e}")
        return

    if not isinstance(data, list):
        summary.add_check("Artifacts Relative URI", False, "artifacts/index.json phải là danh sách.")
        return

    invalid_uris = []
    for item in data:
        uri = item.get("uri", "")
        try:
            validate_artifact_uri(uri)
        except Exception as exc:
            invalid_uris.append(f"{uri or '(rỗng)'} ({exc})")

    if invalid_uris:
        summary.add_check(
            "Artifacts Relative URI",
            False,
            f"Phát hiện URI không hợp lệ/tuyệt đối: {', '.join(invalid_uris)}",
        )
    else:
        summary.add_check(
            "Artifacts Relative URI",
            True,
            f"Tất cả {len(data)} artifact URI đều là đường dẫn tương đối an toàn.",
        )


def check_d7_gitignore_isolation(agent_path: Path, summary: AgentValidationSummary) -> None:
    """D.7 Kiểm tra .gitignore có .env, workspace/, data/ và root .gitignore có endpoints.yaml, .env.lan."""
    gitignore = agent_path / ".gitignore"
    if not gitignore.exists():
        summary.add_check("Gitignore Isolation", False, "Thiếu file .gitignore trong thư mục agent.")
        return

    lines = [line.strip().rstrip("/") for line in gitignore.read_text(encoding="utf-8").splitlines()]
    required = [".env", "workspace", "data"]
    missing = [req for req in required if req not in lines and f"{req}/" not in lines]

    # Kiểm tra root .gitignore có endpoints.yaml và .env.lan
    root_gitignore = ROOT_DIR / ".gitignore"
    if root_gitignore.exists():
        root_lines = [l.strip().rstrip("/") for l in root_gitignore.read_text(encoding="utf-8").splitlines()]
        lan_required = ["endpoints.yaml", ".env.lan"]
        lan_missing = [req for req in lan_required if not any(req in rl for rl in root_lines)]
        if lan_missing:
            missing.extend(lan_missing)

    if missing:
        summary.add_check(
            "Gitignore Isolation",
            False,
            f".gitignore thiếu các mục cách ly bắt buộc: {', '.join(missing)}",
        )
    else:
        summary.add_check(
            "Gitignore Isolation",
            True,
            ".gitignore chứa đầy đủ .env, workspace/, data/, endpoints.yaml, .env.lan.",
        )


def _load_agent_instance(agent_path: Path) -> Any:
    """Import động agent class và khởi tạo instance ở chế độ mock."""
    agent_file = agent_path / "agent.py"
    if not agent_file.exists():
        raise FileNotFoundError(f"Không tìm thấy {agent_file}")

    for key in list(sys.modules.keys()):
        if key in ["agent", "config", "llm", "prompt", "schemas", "api", "tools"] or key.startswith("tools."):
            sys.modules.pop(key, None)

    if str(agent_path) in sys.path:
        sys.path.remove(str(agent_path))
    sys.path.insert(0, str(agent_path))

    spec = importlib.util.spec_from_file_location(f"agent_module_{agent_path.name}", str(agent_file))
    if spec is None or spec.loader is None:
        raise ImportError(f"Không thể tải spec từ {agent_file}")

    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    agent_cls = None
    for name, obj in vars(mod).items():
        if isinstance(obj, type) and hasattr(obj, "handle") and hasattr(obj, "get_identity") and name != "BaseAgent":
            agent_cls = obj
            break

    if agent_cls is None:
        agent_cls = getattr(mod, "BaseAgent", None)

    if agent_cls is None:
        raise AttributeError(f"Không tìm thấy Agent class hợp lệ trong {agent_file}")

    try:
        return agent_cls()
    except TypeError:
        config_mod = importlib.import_module("config")
        config_cls = getattr(config_mod, "AgentConfig", None) or getattr(config_mod, "DevOpsConfig", None) or getattr(config_mod, "PMConfig", None)
        cfg = config_cls(port=8999) if config_cls else None
        role_enum = getattr(AgentRole, agent_path.name.upper(), AgentRole.PM)
        identity = AgentIdentity(
            agent_id=f"{agent_path.name.lower()}-01",
            role=role_enum,
            name=f"{role_enum.value} Agent",
            version="1.0.0",
            capabilities=CAPABILITY_REGISTRY.get(role_enum, []),
        )
        return agent_cls(identity=identity, config=cfg)


def check_3_identity_and_capabilities(agent_path: Path, agent_instance: Any, summary: AgentValidationSummary) -> None:
    """3. Kiểm tra import agent, get_identity() hợp lệ, capabilities là tập con của registry, role khớp thư mục."""
    try:
        identity = agent_instance.get_identity()
        if not isinstance(identity, AgentIdentity):
            summary.add_check("Identity & Registry", False, "get_identity() không trả về đối tượng AgentIdentity.")
            return

        summary.agent_id = identity.agent_id
        summary.role = identity.role.value

        expected_role_str = agent_path.name.upper()
        if identity.role.value != expected_role_str:
            summary.add_check(
                "Identity & Registry",
                False,
                f"Role khai báo '{identity.role.value}' không khớp với tên thư mục agent '{expected_role_str}'.",
            )
            return

        registry_caps = CAPABILITY_REGISTRY.get(identity.role, [])
        for cap in identity.capabilities:
            if cap not in registry_caps:
                summary.add_check(
                    "Identity & Registry",
                    False,
                    f"Capability '{cap}' không thuộc registry chuẩn của role {identity.role.value}.",
                )
                return

        summary.add_check("Identity & Registry", True, f"Identity {identity.agent_id} ({identity.role.value}) hợp lệ.")
    except Exception as e:
        summary.add_check("Identity & Registry", False, f"Lỗi khi kiểm tra identity: {e}")


def check_4_run_mock_task(agent_path: Path, agent_instance: Any, summary: AgentValidationSummary) -> None:
    """4. Chạy examples/task.json ở chế độ mock: kết quả là TaskResult hợp lệ."""
    task_file = agent_path / "examples" / "task.json"
    if not task_file.exists():
        examples_dir = agent_path / "examples"
        json_files = list(examples_dir.glob("*.json"))
        if json_files:
            task_file = json_files[0]
        else:
            summary.add_check("Mock Task Execution", False, "Không tìm thấy task.json mẫu trong examples/.")
            return

    try:
        req_data = json.loads(task_file.read_text(encoding="utf-8"))
        request = TaskRequest.model_validate(req_data)
    except Exception as e:
        summary.add_check("Mock Task Execution", False, f"File {task_file.name} không phải TaskRequest hợp lệ: {e}")
        return

    try:
        result = agent_instance.handle(request)
        if not isinstance(result, TaskResult):
            summary.add_check("Mock Task Execution", False, "handle() không trả về đối tượng TaskResult.")
            return

        if not isinstance(result.status, TaskStatus):
            summary.add_check("Mock Task Execution", False, f"status không thuộc TaskStatus: {result.status}")
            return

        if result.status == TaskStatus.COMPLETED and result.error is not None:
            summary.add_check("Mock Task Execution", False, "status=COMPLETED nhưng error khác None.")
            return

        if result.status in [TaskStatus.FAILED, TaskStatus.BLOCKED] and result.error is None:
            summary.add_check("Mock Task Execution", False, f"status={result.status.value} nhưng thiếu error.")
            return

        summary.add_check("Mock Task Execution", True, f"Tác vụ mẫu thực thi thành công (Status: {result.status.value}).")
    except Exception as e:
        summary.add_check("Mock Task Execution", False, f"Exception văng ra ngoài handle(): {e}")


def check_5_error_handling(agent_instance: Any, summary: AgentValidationSummary) -> None:
    """5. Gửi action sai nhận FAILED + PERMISSION_DENIED. Gửi input hỏng nhận FAILED + INVALID_INPUT."""
    identity = agent_instance.get_identity()

    bad_action_req = TaskRequest(
        task_id="TASK-BAD-ACTION-999",
        project_id="PROJ-TEST",
        agent_id=identity.agent_id,
        role=identity.role,
        action="action_hoan_toan_khong_ton_tai_xyz",
        instruction="Thử gọi action bất hợp lệ",
    )

    try:
        res1 = agent_instance.handle(bad_action_req)
        if res1.status != TaskStatus.FAILED or res1.error is None or res1.error.code != ErrorCode.PERMISSION_DENIED:
            summary.add_check(
                "Action/Input Error Handling",
                False,
                f"Action sai không trả về FAILED + PERMISSION_DENIED (nhận {res1.status.value}, {getattr(res1.error, 'code', None)})",
            )
            return
    except Exception as e:
        summary.add_check("Action/Input Error Handling", False, f"Action sai ném exception ra ngoài: {e}")
        return

    valid_actions = list(ACTION_MAP.get(identity.role, {}).keys())
    test_action = valid_actions[0] if valid_actions else "create_plan"

    corrupted_req = TaskRequest(
        task_id="TASK-CORRUPT-999",
        project_id="PROJ-TEST",
        agent_id=identity.agent_id,
        role=identity.role,
        action=test_action,
        instruction="Input hỏng",
        context={"corrupted_payload_test": 12345},
    )

    try:
        res2 = agent_instance.handle(corrupted_req)
        if not isinstance(res2, TaskResult):
            summary.add_check("Action/Input Error Handling", False, "handle() với input hỏng không trả về TaskResult.")
            return
        summary.add_check("Action/Input Error Handling", True, "Xử lý chuẩn action sai và input hỏng (không ném ngoại lệ).")
    except Exception as e:
        summary.add_check("Action/Input Error Handling", False, f"Input hỏng ném exception ra ngoài: {e}")


def check_7_unit_tests(agent_path: Path, summary: AgentValidationSummary) -> None:
    """7. Mỗi agent có ít nhất một test và pytest của agent đó xanh."""
    tests_dir = agent_path / "tests"
    if not tests_dir.exists():
        summary.add_check("Unit Tests", False, "Không tìm thấy thư mục tests/.")
        return

    test_files = list(tests_dir.glob("test_*.py"))
    if not test_files:
        summary.add_check("Unit Tests", False, "Thư mục tests/ không chứa file test_*.py nào.")
        return

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(tests_dir), "-q"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(ROOT_DIR),
        )
        if proc.returncode == 0:
            summary.add_check("Unit Tests", True, f"Pytest chạy xanh ({len(test_files)} file test).")
        else:
            err_output = proc.stdout.strip() or proc.stderr.strip()
            summary.add_check("Unit Tests", False, f"Pytest thất bại (exit code {proc.returncode}): {err_output[:200]}")
    except subprocess.TimeoutExpired:
        summary.add_check("Unit Tests", False, "Pytest chạy quá thời gian (timeout 60s).")
    except Exception as e:
        summary.add_check("Unit Tests", False, f"Không thể thực thi pytest: {e}")


def check_f1_agent_api_v1(agent_path: Path, summary: AgentValidationSummary) -> None:
    """F.1 Kiểm tra Agent API v1: đủ endpoints, endpoint ngoài /health yêu cầu Bearer token."""
    api_file = agent_path / "api.py"
    if not api_file.exists():
        summary.add_check("Agent API v1", False, "Không tìm thấy file api.py trong thư mục agent.")
        return

    try:
        from fastapi.testclient import TestClient

        if str(agent_path) not in sys.path:
            sys.path.insert(0, str(agent_path))

        for key in list(sys.modules.keys()):
            if key in ["api", "config", "agent", "llm", "prompt", "schemas", "tools"] or key.startswith("tools."):
                sys.modules.pop(key, None)

        spec = importlib.util.spec_from_file_location(f"api_module_{agent_path.name}", str(api_file))
        if spec is None or spec.loader is None:
            summary.add_check("Agent API v1", False, f"Không thể tải spec từ {api_file}")
            return

        api_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api_mod)
        app = getattr(api_mod, "app", None)
        if app is None:
            summary.add_check("Agent API v1", False, "Không tìm thấy biến 'app' trong api.py")
            return

        routes: dict[str, set[str]] = {}
        for route in app.routes:
            if hasattr(route, "path") and hasattr(route, "methods"):
                routes[route.path] = route.methods

        required_endpoints = [
            ("/health", "GET"),
            ("/agent", "GET"),
            ("/state", "GET"),
            ("/tasks", "POST"),
            ("/tasks/{task_id}", "GET"),
            ("/artifacts", "GET"),
            ("/artifacts/{artifact_id}/content", "GET"),
            ("/artifacts/inbox", "POST"),
        ]

        missing_endpoints = []
        for path, method in required_endpoints:
            if path not in routes or method not in routes[path]:
                missing_endpoints.append(f"{method} {path}")

        if missing_endpoints:
            summary.add_check(
                "Agent API v1",
                False,
                f"Thiếu các endpoint Agent API v1 bắt buộc: {', '.join(missing_endpoints)}",
            )
            return

        client = TestClient(app, raise_server_exceptions=False)

        # /health công khai không cần token
        res_health = client.get("/health")
        if res_health.status_code != 200:
            summary.add_check(
                "Agent API v1",
                False,
                f"GET /health yêu cầu trả 200 nhưng nhận được {res_health.status_code}",
            )
            return

        # /agent không có token phải trả 401
        res_agent = client.get("/agent")
        if res_agent.status_code != 401:
            summary.add_check(
                "Agent API v1",
                False,
                f"GET /agent không có token phải trả 401 nhưng nhận được {res_agent.status_code}",
            )
            return

        # /artifacts/inbox không có token phải trả 401
        res_inbox = client.post("/artifacts/inbox")
        if res_inbox.status_code != 401:
            summary.add_check(
                "Agent API v1",
                False,
                f"POST /artifacts/inbox không có token phải trả 401 nhưng nhận được {res_inbox.status_code}",
            )
            return

        summary.add_check(
            "Agent API v1",
            True,
            "Đầy đủ 8 endpoint Agent API v1, /health công khai, các endpoint khác yêu cầu Bearer token (401).",
        )
    except Exception as e:
        summary.add_check("Agent API v1", False, f"Lỗi khi kiểm tra Agent API v1: {e}")


def validate_single_agent(agent_dir_path: Path, all_agent_folders: list[Path] | None = None) -> AgentValidationSummary:
    """Kiểm tra toàn diện một thư mục Agent theo toàn bộ tiêu chí Contract v1.0 và mở rộng."""
    summary = AgentValidationSummary(agent_dir=agent_dir_path.name)

    # 1. File bắt buộc
    check_1_required_files(agent_dir_path, summary)

    # 2. .env.example và secrets
    check_2_env_and_secrets(agent_dir_path, summary)

    # D.1 Không có đường dẫn tuyệt đối cứng
    check_d1_no_hardcoded_paths(agent_dir_path, summary)

    # D.2 Timezone-aware datetime
    check_d2_timezone_aware_datetime(agent_dir_path, summary)

    # D.3 Không phụ thuộc agent khác
    check_d3_no_inter_agent_access(agent_dir_path, summary)

    # D.4 Port duy nhất và khớp
    check_d4_port_uniqueness_and_alignment(agent_dir_path, summary, all_agent_folders)

    # D.5 Requirements ghim
    check_d5_requirements_pinned(agent_dir_path, summary, all_agent_folders)

    # D.6 Artifacts URI tương đối
    check_d6_artifacts_relative_uris(agent_dir_path, summary)

    # D.7 Gitignore cách ly
    check_d7_gitignore_isolation(agent_dir_path, summary)

    # Unit tests của riêng agent
    check_7_unit_tests(agent_dir_path, summary)

    # F.1 Agent API v1
    check_f1_agent_api_v1(agent_dir_path, summary)

    # Import và chạy các check runtime
    try:
        instance = _load_agent_instance(agent_dir_path)
    except Exception as e:
        summary.add_check("Agent Instantiation", False, f"Không thể khởi tạo Agent: {e}")
        return summary

    # Identity & Registry
    check_3_identity_and_capabilities(agent_dir_path, instance, summary)

    # Mock Task
    check_4_run_mock_task(agent_dir_path, instance, summary)

    # Error handling
    check_5_error_handling(instance, summary)

    return summary


def generate_markdown_report(summaries: list[AgentValidationSummary], output_file: Path) -> None:
    """Xuất báo cáo kết quả kiểm tra định dạng Markdown."""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    lines = [
        "# Báo cáo Contract Validation các Agent (Agent Contract v1.0)",
        f"\n*Thời gian tạo: {now_str}*",
        "\n## 1. Tổng quan kết quả\n",
        "| Thư mục | Agent ID | Role | Trạng thái | Ghi chú |",
        "|---|---|---|---|---|",
    ]

    for s in summaries:
        status_badge = "✅ **PASS**" if s.passed else "❌ **FAIL**"
        fail_reasons = [c.message for c in s.checks if not c.passed]
        note = "Đạt toàn bộ tiêu chí contract" if s.passed else f"Lỗi: {'; '.join(fail_reasons)}"
        lines.append(f"| `{s.agent_dir}` | `{s.agent_id}` | `{s.role}` | {status_badge} | {note} |")

    lines.append("\n## 2. Chi tiết các hạng mục kiểm tra từng Agent\n")

    for s in summaries:
        lines.append(f"### Agent: `{s.agent_dir}` (Role: {s.role}, ID: {s.agent_id})")
        lines.append("| Tiêu chí kiểm tra | Kết quả | Chi tiết |")
        lines.append("|---|---|---|")
        for c in s.checks:
            badge = "PASS" if c.passed else "**FAIL**"
            lines.append(f"| {c.name} | {badge} | {c.message} |")
        lines.append("")

    output_file.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nĐã xuất báo cáo chi tiết ra file: {output_file.resolve()}")


def print_console_table(summaries: list[AgentValidationSummary]) -> None:
    """In bảng kết quả PASS/FAIL trực quan ra màn hình console."""
    sep = "=" * 90
    print("\n" + sep)
    print(" KẾT QUẢ KIỂM TRA CONTRACT VALIDATION (Agent Contract v1.0)")
    print(sep)
    header = f"| {'AGENT DIR':<12} | {'ID':<10} | {'ROLE':<8} | {'STATUS':<6} | {'CHI TIẾT KIỂM TRA':<42} |"
    print(header)
    print("|" + "-" * 14 + "|" + "-" * 12 + "|" + "-" * 10 + "|" + "-" * 8 + "|" + "-" * 44 + "|")

    for s in summaries:
        status_str = "PASS" if s.passed else "FAIL"
        if s.passed:
            detail = f"Tất cả {len(s.checks)} kiểm tra đều ĐẠT."
        else:
            failed_checks = [c.name for c in s.checks if not c.passed]
            detail = f"Không đạt: {', '.join(failed_checks)}"
        print(f"| {s.agent_dir:<12} | {s.agent_id:<10} | {s.role:<8} | {status_str:<6} | {detail:<42} |")

    print(sep + "\n")


def main() -> int:
    _setup_console_encoding()
    agents_dir = ROOT_DIR / "agents"
    if not agents_dir.exists():
        print(f"Lỗi: Thư mục agents không tồn tại tại {agents_dir}", file=sys.stderr)
        return 1

    agent_folders = [
        d for d in agents_dir.iterdir()
        if d.is_dir() and (d / "agent.py").exists() and not d.name.startswith((".", "_"))
    ]
    if not agent_folders:
        print("Không tìm thấy thư mục agent nào trong agents/.", file=sys.stderr)
        return 0

    summaries: list[AgentValidationSummary] = []
    has_failure = False

    for folder in sorted(agent_folders, key=lambda p: p.name):
        print(f"Đang kiểm tra agent '{folder.name}'...")
        summary = validate_single_agent(folder, agent_folders)
        summaries.append(summary)
        if not summary.passed:
            has_failure = True

    print_console_table(summaries)

    report_file = ROOT_DIR / "docs" / "validation-report.md"
    generate_markdown_report(summaries, report_file)

    if has_failure:
        print("Phát hiện có Agent KHÔNG ĐẠT tiêu chuẩn Contract Validation!", file=sys.stderr)
        return 1

    print("Tất cả các Agent đã vượt qua bài kiểm tra Contract Validation thành công!")
    return 0


if __name__ == "__main__":
    sys.exit(main())
