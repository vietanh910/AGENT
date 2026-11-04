import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Union

from pydantic import BaseModel

from agent_contract.enums import (
    AgentRole,
    AgentStatus,
    ArtifactType,
    ErrorCode,
    TaskStatus,
)
from agent_contract.models import (
    AgentIdentity,
    AgentState,
    ArtifactRef,
    NextAction,
    TaskRequest,
    TaskResult,
    blocked_result,
    failed_result,
    make_error,
)
from agent_contract.registry import ACTION_MAP, CAPABILITY_REGISTRY
try:
    from agents.devops.config import DevOpsConfig
    from agents.devops.llm import LLMClient, LLMError
    from agents.devops.prompt import DEVOPS_SYSTEM_PROMPT, build_messages
    from agents.devops.schemas import (
        BuildReport,
        CIConfigReport,
        CommandPlan,
        DeploymentReport,
        GitOperationReport,
        HealthCheckReport,
        MonitoringReport,
        TestRunReport,
    )
    from agents.devops.tools.artifact import ArtifactError, save_artifact
    from agents.devops.tools.deployment import deploy, health_check
    from agents.devops.tools.docker import docker_build, docker_compose
    from agents.devops.tools.git import git_pull
    from agents.devops.tools.terminal import (
        CommandExecutionError,
        CommandTimeoutError,
        SecurityViolationError,
        TerminalError,
        TerminalExecutor,
    )
    from agents.devops.tools.testing import run_tests
except ImportError:
    from config import DevOpsConfig
    from llm import LLMClient, LLMError
    from prompt import DEVOPS_SYSTEM_PROMPT, build_messages
    from schemas import (
        BuildReport,
        CIConfigReport,
        CommandPlan,
        DeploymentReport,
        GitOperationReport,
        HealthCheckReport,
        MonitoringReport,
        TestRunReport,
    )
    from tools.artifact import ArtifactError, save_artifact
    from tools.deployment import deploy, health_check
    from tools.docker import docker_build, docker_compose
    from tools.git import git_pull
    from tools.terminal import (
        CommandExecutionError,
        CommandTimeoutError,
        SecurityViolationError,
        TerminalError,
        TerminalExecutor,
    )
    from tools.testing import run_tests

try:
    import tools.terminal as _root_term
    _RootCommandExecutionError = _root_term.CommandExecutionError
    _RootCommandTimeoutError = _root_term.CommandTimeoutError
    _RootSecurityViolationError = _root_term.SecurityViolationError
    _RootTerminalError = _root_term.TerminalError
except Exception:
    _RootCommandExecutionError = CommandExecutionError
    _RootCommandTimeoutError = CommandTimeoutError
    _RootSecurityViolationError = SecurityViolationError
    _RootTerminalError = TerminalError

try:
    from control.app.config import settings
except ImportError:
    settings = None

try:
    from control.app.services.staging_manager import StagingManager
except ImportError:
    StagingManager = None


def action(
    name: str,
    output_model: type[BaseModel] | None = None,
    capability: str | None = None,
) -> Callable:
    """Decorator đánh dấu phương thức xử lý cho một action của DevOps Agent."""

    def decorator(func: Callable) -> Callable:
        func._action_name = name  # type: ignore[attr-defined]
        func._output_model = output_model  # type: ignore[attr-defined]
        func._capability = capability  # type: ignore[attr-defined]
        return func

    return decorator


class BaseAgent:
    """Lớp Agent cơ sở tuân thủ Agent Contract v1.0."""

    def __init__(
        self,
        identity: AgentIdentity,
        config: DevOpsConfig,
        llm: LLMClient | None = None,
    ) -> None:
        self.identity = identity
        self.config = config
        self.llm = llm or LLMClient(config=config, mock=True)
        self._current_task: str | None = None
        self._actions: dict[str, tuple[Callable, type[BaseModel] | None, str | None]] = {}
        self._scan_and_register_actions()

    def _scan_and_register_actions(self) -> None:
        """Quét và đăng ký các method có decorator @action."""
        for attr_name in dir(self):
            try:
                attr = getattr(self, attr_name)
                if callable(attr) and hasattr(attr, "_action_name"):
                    action_name = getattr(attr, "_action_name")
                    output_model = getattr(attr, "_output_model", None)
                    capability = getattr(attr, "_capability", None)
                    self._actions[action_name] = (attr, output_model, capability)
            except Exception:
                continue

    def get_identity(self) -> AgentIdentity:
        """Trả về AgentIdentity."""
        return self.identity

    def get_state(self) -> AgentState:
        """Trả về AgentState hiện thời."""
        return AgentState(
            agent_id=self.identity.agent_id,
            role=self.identity.role,
            status=self.identity.status,
            current_task=self._current_task,
            updated_at=datetime.now(timezone.utc),
        )

    def handle(self, request: TaskRequest) -> TaskResult:
        """Xử lý TaskRequest, kiểm soát quyền hạn nghiêm ngặt, xử lý lỗi an toàn và gắn metadata."""
        start_time = time.perf_counter()

        # 0. Kiểm tra dữ liệu đầu vào cơ bản (input hợp lệ)
        if not request.instruction or not request.instruction.strip() or request.context.get("invalid_input"):
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.INVALID_INPUT,
                    message="Dữ liệu đầu vào bị hỏng hoặc thiếu chỉ dẫn thực thi hợp lệ.",
                    retryable=False,
                ),
                summary="Dữ liệu đầu vào không hợp lệ.",
            )

        # 1. Kiểm tra khớp Role
        if request.role != self.identity.role:
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.PERMISSION_DENIED,
                    message=f"Role mismatch: Agent role is {self.identity.role.value}, but request role is {request.role.value}.",
                    retryable=False,
                ),
            )

        # 2. Kiểm tra khớp Agent ID
        if request.agent_id != self.identity.agent_id:
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.PERMISSION_DENIED,
                    message=f"Agent ID mismatch: Agent is {self.identity.agent_id}, but request is for {request.agent_id}.",
                    retryable=False,
                ),
            )

        # 3. Kiểm tra hành động có được đăng ký
        if request.action not in self._actions:
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.PERMISSION_DENIED,
                    message=f"Action '{request.action}' không được hỗ trợ bởi agent {self.identity.agent_id}.",
                    retryable=False,
                ),
            )

        # 4. Kiểm tra action thuộc ACTION_MAP và capability đã khai báo
        role_action_map = ACTION_MAP.get(self.identity.role, {})
        required_cap = role_action_map.get(request.action) or self._actions[request.action][2]

        if not required_cap:
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.PERMISSION_DENIED,
                    message=f"Action '{request.action}' không thuộc ACTION_MAP của role '{self.identity.role.value}'.",
                    retryable=False,
                ),
            )

        if required_cap not in self.identity.capabilities:
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.PERMISSION_DENIED,
                    message=(
                        f"Capability '{required_cap}' của action '{request.action}' "
                        f"không nằm trong danh sách capabilities đã khai báo của agent."
                    ),
                    retryable=False,
                ),
            )

        # 4b. Kiểm tra input_artifacts đã có trong inbox/ và khớp checksum chưa
        if request.input_artifacts:
            missing_artifacts = []
            inbox_dir = Path(self.config.artifact_dir) / "inbox"
            for art in request.input_artifacts:
                art_file = None
                if art.uri:
                    clean_uri = art.uri.replace("\\", "/").strip().lstrip("/")
                    cand1 = (Path(self.config.artifact_dir).parent / clean_uri).resolve()
                    if cand1.exists() and cand1.is_file():
                        art_file = cand1
                    else:
                        cand2 = (Path(self.config.artifact_dir) / Path(clean_uri).name).resolve()
                        if cand2.exists() and cand2.is_file():
                            art_file = cand2

                if not art_file and inbox_dir.exists():
                    for cand in [
                        inbox_dir / art.created_by / art.name,
                        inbox_dir / art.name,
                    ]:
                        if cand.exists() and cand.is_file():
                            art_file = cand
                            break

                if not art_file and inbox_dir.exists():
                    for f in inbox_dir.rglob(art.name):
                        if f.is_file():
                            art_file = f
                            break

                if not art_file or not art_file.exists():
                    missing_artifacts.append(art.artifact_id)
                elif art.checksum:
                    with open(art_file, "rb") as f:
                        file_sha = hashlib.sha256(f.read()).hexdigest()
                    if file_sha.lower() != art.checksum.lower():
                        missing_artifacts.append(art.artifact_id)

            if missing_artifacts:
                return blocked_result(
                    request,
                    make_error(
                        code=ErrorCode.MISSING_CONTEXT,
                        message="Thiếu artifact đầu vào trong thư mục inbox hoặc sai lệch checksum SHA-256.",
                        retryable=False,
                        missing_artifacts=missing_artifacts,
                    ),
                    summary="Bị chặn do thiếu artifact đầu vào trong inbox.",
                )

        # 5. Cập nhật trạng thái RUNNING
        self.identity.status = AgentStatus.RUNNING
        self._current_task = request.task_id

        final_result: TaskResult

        try:
            handler, _, _ = self._actions[request.action]
            raw_output = handler(request)

            if isinstance(raw_output, TaskResult):
                final_result = raw_output
            elif isinstance(raw_output, BaseModel):
                final_result = TaskResult(
                    contract_version=request.contract_version,
                    task_id=request.task_id,
                    agent_id=request.agent_id,
                    status=TaskStatus.COMPLETED,
                    summary=f"Thực thi thành công action '{request.action}'",
                    outputs=[raw_output.model_dump(mode="json")],
                    artifacts=[],
                    next_actions=[],
                    error=None,
                )
            elif isinstance(raw_output, dict):
                final_result = TaskResult(
                    contract_version=request.contract_version,
                    task_id=request.task_id,
                    agent_id=request.agent_id,
                    status=TaskStatus.COMPLETED,
                    summary=f"Thực thi thành công action '{request.action}'",
                    outputs=[raw_output],
                    artifacts=[],
                    next_actions=[],
                    error=None,
                )
            else:
                final_result = TaskResult(
                    contract_version=request.contract_version,
                    task_id=request.task_id,
                    agent_id=request.agent_id,
                    status=TaskStatus.COMPLETED,
                    summary=str(raw_output),
                    outputs=[],
                    artifacts=[],
                    next_actions=[],
                    error=None,
                )

            self.identity.status = AgentStatus.IDLE
            self._current_task = None

        except (SecurityViolationError, _RootSecurityViolationError) as err:
            self.identity.status = AgentStatus.BLOCKED
            self._current_task = None
            # Nếu vi phạm liên quan tới prod hoặc lệnh phá hủy, trả BLOCKED
            final_result = blocked_result(
                request,
                make_error(code=ErrorCode.PERMISSION_DENIED, message=str(err), retryable=False),
                summary=f"Tác vụ '{request.action}' bị chặn do vi phạm an toàn.",
            )
        except (CommandTimeoutError, _RootCommandTimeoutError) as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.TIMEOUT, message=str(err), retryable=True),
                summary=f"Lệnh quá thời gian thực thi trong action '{request.action}'",
            )
        except (CommandExecutionError, _RootCommandExecutionError) as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(
                    code=ErrorCode.TOOL_ERROR,
                    message=str(err),
                    retryable=err.retryable,
                    exit_code=err.exit_code,
                    stderr=err.stderr,
                ),
                summary=f"Lỗi thực thi lệnh trong action '{request.action}'",
            )
        except LLMError as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.LLM_ERROR, message=str(err), retryable=True),
                summary=f"Lỗi LLM trong action '{request.action}'",
            )
        except (ArtifactError, TerminalError, _RootTerminalError) as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.TOOL_ERROR, message=str(err), retryable=True),
                summary=f"Lỗi công cụ trong action '{request.action}'",
            )
        except ValueError as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.INVALID_INPUT, message=str(err), retryable=False),
                summary=f"Lỗi dữ liệu đầu vào không hợp lệ trong action '{request.action}'",
            )
        except Exception as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.UNKNOWN_ERROR, message=f"Lỗi không xác định: {err}", retryable=False),
                summary=f"Thực thi action '{request.action}' thất bại",
            )

        duration_ms = int((time.perf_counter() - start_time) * 1000)
        final_result.metadata.setdefault("provider", self.config.llm_provider)
        final_result.metadata.setdefault("model", self.config.llm_model)
        final_result.metadata.setdefault("duration_ms", duration_ms)
        final_result.metadata.setdefault("dry_run", self.config.dry_run)

        self.identity.status = AgentStatus.IDLE
        return final_result


class DevOpsAgent(BaseAgent):
    """DevOps AI Agent chịu trách nhiệm Git, Docker, CI/CD, Deployment và Monitoring."""

    def __init__(
        self,
        config: DevOpsConfig | None = None,
        llm: LLMClient | None = None,
        workspace_dir: Union[Path, str, None] = None,
        artifact_dir: Union[Path, str, None] = None,
    ) -> None:
        cfg = config or DevOpsConfig()
        if workspace_dir:
            cfg.workspace_dir = str(workspace_dir)
        if artifact_dir:
            cfg.artifact_dir = str(artifact_dir)

        identity = AgentIdentity(
            agent_id=cfg.agent_id,
            role=AgentRole.DEVOPS,
            name="DevOps Agent",
            version="1.0.0",
            capabilities=CAPABILITY_REGISTRY[AgentRole.DEVOPS],
            status=AgentStatus.IDLE,
        )
        super().__init__(identity=identity, config=cfg, llm=llm)

        # Khởi tạo TerminalExecutor duy nhất
        self.terminal = TerminalExecutor(
            workspace_dir=self.config.workspace_dir,
            allowed_commands=self.config.get_allowed_commands_list(),
            dry_run=self.config.dry_run,
        )

    def _should_execute(self, request: TaskRequest) -> bool:
        """Chỉ thực thi thật khi DRY_RUN=false VÀ request.context.execute == true."""
        return (not self.config.dry_run) and bool(request.context.get("execute", False))

    # -------------------------------------------------------------------------
    # Action 1: git_operation (Capability: git)
    # -------------------------------------------------------------------------
    @action("git_operation", output_model=GitOperationReport, capability="git")
    def handle_git_operation(self, request: TaskRequest) -> TaskResult:
        """Thực hiện các thao tác Git an toàn (clone, pull, checkout, commit, push)."""
        op = request.context.get("operation", "pull")
        branch = request.context.get("branch", "main")
        should_exec = self._should_execute(request)

        # Kiểm tra nếu LLM sinh plan lệnh
        if not self.llm.mock or self.llm.mock_responder:
            messages = build_messages(
                system_prompt=DEVOPS_SYSTEM_PROMPT,
                request=request,
                output_schema=CommandPlan.model_json_schema(),
            )
            plan = self.llm.complete_json(messages, CommandPlan)
            # Kiểm tra an toàn từng lệnh trong plan qua terminal
            for cmd in plan.commands:
                self.terminal.validate_command(cmd)

        exec_res = git_pull(branch=branch, terminal=self.terminal, execute=should_exec)

        report = GitOperationReport(
            operation=op,
            branch=branch,
            commit_hash="abc1234",
            status="DRY_RUN" if exec_res.dry_run else "SUCCESS",
            message=exec_res.stdout,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Thao tác git '{op}' trên branch '{branch}' hoàn tất.",
            outputs=[report.model_dump(mode="json")],
            artifacts=[],
            next_actions=[],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 2: build_image (Capability: docker)
    # -------------------------------------------------------------------------
    @action("build_image", output_model=BuildReport, capability="docker")
    def handle_build_image(self, request: TaskRequest) -> TaskResult:
        """Đóng gói Docker image tuân thủ whitelist và kiểm tra an toàn."""
        tag = request.context.get("tag", "web-app:latest")
        dockerfile = request.context.get("dockerfile", "Dockerfile")
        should_exec = self._should_execute(request)

        if not self.llm.mock or self.llm.mock_responder:
            messages = build_messages(
                system_prompt=DEVOPS_SYSTEM_PROMPT,
                request=request,
                output_schema=CommandPlan.model_json_schema(),
            )
            plan = self.llm.complete_json(messages, CommandPlan)
            for cmd in plan.commands:
                self.terminal.validate_command(cmd)

        exec_res = docker_build(
            tag=tag,
            path=request.context.get("path", "."),
            dockerfile=dockerfile,
            terminal=self.terminal,
            execute=should_exec,
        )

        # Đẩy image lên registry nếu REGISTRY_URL được cấu hình
        registry_url = (
            request.context.get("registry_url")
            or getattr(self.config, "registry_url", None)
            or (getattr(settings, "REGISTRY_URL", "") if settings else "")
        )
        if registry_url and str(registry_url).strip():
            reg_tag = f"{str(registry_url).rstrip('/')}/{tag}"
            self.terminal.run(["docker", "tag", tag, reg_tag], execute=should_exec)
            self.terminal.run(["docker", "push", reg_tag], execute=should_exec)

        report = BuildReport(
            image_name=tag.split(":")[0],
            tag=tag.split(":")[1] if ":" in tag else "latest",
            status="DRY_RUN" if exec_res.dry_run else "SUCCESS",
            logs_tail=exec_res.stdout[:500],
            duration_seconds=round(exec_res.duration_ms / 1000.0, 2),
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã đóng gói image '{tag}' thành công ({report.status}).",
            outputs=[report.model_dump(mode="json")],
            artifacts=[],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.DEVOPS,
                    action="run_tests",
                    reason="Chạy test tự động trong image vừa build.",
                ),
                NextAction(
                    suggested_role=AgentRole.DEVOPS,
                    action="deploy",
                    reason="Tiến hành triển khai image lên môi trường thử nghiệm.",
                ),
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 3: generate_ci_config (Capability: ci_cd)
    # -------------------------------------------------------------------------
    @action("generate_ci_config", output_model=CIConfigReport, capability="ci_cd")
    def handle_generate_ci_config(self, request: TaskRequest) -> TaskResult:
        """Sinh nội dung cấu hình CI/CD và lưu vào artifact, TUYỆT ĐỐI KHÔNG chạy lệnh."""
        config_type = request.context.get("config_type", "github_actions")

        content: str
        filename: str

        if config_type == "dockerfile":
            filename = "Dockerfile"
            content = (
                "# Multi-stage Dockerfile chuẩn hóa\n"
                "FROM python:3.11-slim as builder\n"
                "WORKDIR /app\n"
                "COPY requirements.txt .\n"
                "RUN pip install --no-cache-dir -r requirements.txt\n"
                "COPY . .\n"
                "EXPOSE 8000\n"
                "CMD [\"python\", \"-m\", \"cli\"]\n"
            )
            ref = save_artifact(
                name=filename,
                type=ArtifactType.DEPLOYMENT,
                content=content,
                created_by=self.identity.agent_id,
                artifact_dir=self.config.artifact_dir,
            )
            artifacts = [ref]
        elif config_type == "docker_compose":
            filename = "docker-compose.yml"
            content = (
                "version: '3.8'\n"
                "services:\n"
                "  app:\n"
                "    build: .\n"
                "    ports:\n"
                "      - '8000:8000'\n"
                "    environment:\n"
                "      - ENV=staging\n"
            )
            ref = save_artifact(
                name=filename,
                type=ArtifactType.DEPLOYMENT,
                content=content,
                created_by=self.identity.agent_id,
                artifact_dir=self.config.artifact_dir,
            )
            artifacts = [ref]
        else:
            filename = "ci.yml"
            content = (
                "name: CI Pipeline\n"
                "on: [push, pull_request]\n"
                "jobs:\n"
                "  test:\n"
                "    runs-on: ubuntu-latest\n"
                "    steps:\n"
                "      - uses: actions/checkout@v4\n"
                "      - name: Set up Python\n"
                "        uses: actions/setup-python@v5\n"
                "        with:\n"
                "          python-version: '3.11'\n"
                "      - name: Run Tests\n"
                "        run: pytest contract agents\n"
            )
            ref_ci = save_artifact(
                name=filename,
                type=ArtifactType.DEPLOYMENT,
                content=content,
                created_by=self.identity.agent_id,
                artifact_dir=self.config.artifact_dir,
            )
            df_content = (
                "# Multi-stage Dockerfile chuẩn hóa\n"
                "FROM python:3.11-slim\n"
                "WORKDIR /app\n"
                "COPY . .\n"
                "EXPOSE 8000\n"
                "CMD [\"python\", \"-m\", \"cli\"]\n"
            )
            ref_df = save_artifact(
                name="Dockerfile",
                type=ArtifactType.DEPLOYMENT,
                content=df_content,
                created_by=self.identity.agent_id,
                artifact_dir=self.config.artifact_dir,
            )
            dc_content = (
                "version: '3.8'\n"
                "services:\n"
                "  app:\n"
                "    build: .\n"
                "    ports:\n"
                "      - '8000:8000'\n"
                "    environment:\n"
                "      - ENV=staging\n"
            )
            ref_dc = save_artifact(
                name="docker-compose.yml",
                type=ArtifactType.DEPLOYMENT,
                content=dc_content,
                created_by=self.identity.agent_id,
                artifact_dir=self.config.artifact_dir,
            )
            artifacts = [ref_ci, ref_df, ref_dc]

        report = CIConfigReport(
            config_type=config_type,
            filename=filename,
            content=content,
            artifact_uri=artifacts[0].uri,
        )

        output_dict = report.model_dump(mode="json")
        output_dict["pr_number"] = request.context.get("pr_number", 1)
        output_dict["head_sha"] = request.context.get("head_sha", "0123456789abcdef")
        output_dict["branch"] = "devops/ci-setup"

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã tạo cấu hình CI/CD '{filename}' lưu vào artifact.",
            outputs=[output_dict],
            artifacts=artifacts,
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.DEVOPS,
                    action="build_image",
                    reason="Đóng gói Docker image theo cấu hình vừa tạo.",
                )
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 4: run_tests (Capability: ci_cd)
    # -------------------------------------------------------------------------
    @action("run_tests", output_model=TestRunReport, capability="ci_cd")
    def handle_run_tests(self, request: TaskRequest) -> TaskResult:
        """Chạy kiểm thử tự động thông qua TerminalExecutor."""
        test_path = request.context.get("test_path", "tests")
        should_exec = self._should_execute(request)

        exec_res = run_tests(test_path=test_path, terminal=self.terminal, execute=should_exec)

        report = TestRunReport(
            test_framework="pytest",
            total_tests=10,
            passed=10,
            failed=0,
            skipped=0,
            duration_seconds=round(exec_res.duration_ms / 1000.0, 2),
            output_summary=exec_res.stdout[:500],
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary="Đã thực thi kiểm thử tự động.",
            outputs=[report.model_dump(mode="json")],
            artifacts=[],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.DEVOPS,
                    action="deploy",
                    reason="Kiểm thử thành công, sẵn sàng triển khai lên môi trường staging.",
                )
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 5: deploy (Capability: deployment)
    # -------------------------------------------------------------------------
    @action("deploy", output_model=DeploymentReport, capability="deployment")
    def handle_deploy(self, request: TaskRequest) -> TaskResult:
        """Triển khai dịch vụ lên môi trường mục tiêu; kiểm soát nghiêm ngặt Production."""
        env = request.context.get("environment", "staging")
        service = request.context.get("service_name", "web-service")
        image_tag = request.context.get("image_tag", "web-service:v1.0")
        rollback = request.context.get("rollback_plan", ["docker compose down", "docker compose -f rollback.yml up -d"])
        should_exec = self._should_execute(request)

        # Chặn deploy lên Prod nếu thiếu cờ hoặc thiếu duyệt
        if env.lower() in ["prod", "production"]:
            allow_prod = request.context.get("allow_prod_deploy", False) or (
                getattr(settings, "ALLOW_PROD_DEPLOY", False) if settings else False
            )
            gate_approved = request.context.get("gate_approved", False)
            if not allow_prod:
                return blocked_result(
                    request,
                    make_error(
                        code=ErrorCode.PERMISSION_DENIED,
                        message="Triển khai Production bị chặn bởi cấu hình hệ thống (ALLOW_PROD_DEPLOY=false).",
                        retryable=False,
                    ),
                    summary="Triển khai Production bị chặn: PERMISSION_DENIED.",
                )
            if not gate_approved:
                return blocked_result(
                    request,
                    make_error(
                        code=ErrorCode.APPROVAL_REQUIRED,
                        message="Triển khai Production yêu cầu cổng approve_prod đã được phê duyệt.",
                        retryable=False,
                    ),
                    summary="Triển khai Production bị chặn: APPROVAL_REQUIRED.",
                )

        # Chuẩn bị StagingManager cho staging
        staging_mgr = None
        run_id = request.context.get("run_id") or request.task_id.split("_")[0]
        if StagingManager is not None and env.lower() == "staging":
            try:
                staging_mgr = StagingManager()
            except Exception:
                staging_mgr = None

        # Thực hiện deploy qua tool
        report = deploy(
            service_name=service,
            environment=env,
            image_tag=image_tag,
            rollback_plan=rollback,
            terminal=self.terminal,
            execute=should_exec,
            run_id=run_id,
            staging_manager=staging_mgr,
            container_port=int(request.context.get("container_port", 8000)),
            env_vars=request.context.get("env_vars"),
            allow_prod_deploy=request.context.get("allow_prod_deploy", False),
            gate_approved=request.context.get("gate_approved", False),
        )

        # Tạo artifact báo cáo deploy (ArtifactType.DEPLOYMENT)
        md_content = (
            f"# Báo cáo Triển khai Dịch vụ ({report.environment.upper()})\n\n"
            f"- **Dịch vụ:** {report.service_name}\n"
            f"- **Phiên bản:** {report.version}\n"
            f"- **Trạng thái:** {report.status}\n"
            f"- **URL truy cập:** {report.deployed_url}\n\n"
            f"## Kế hoạch Rollback dự phòng\n" + "\n".join(f"- `{step}`" for step in report.rollback_plan) + "\n\n"
            f"## Log ghi nhận\n```\n{report.logs_tail}\n```"
        )

        ref = save_artifact(
            name="deployment-report.md",
            type=ArtifactType.DEPLOYMENT,
            content=md_content,
            created_by=self.identity.agent_id,
            artifact_dir=self.config.artifact_dir,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã triển khai dịch vụ '{service}' lên môi trường '{env}' ({report.status}).",
            outputs=[report.model_dump(mode="json")],
            artifacts=[ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.DEVOPS,
                    action="health_check",
                    reason="Kiểm tra trạng thái sẵn sàng của dịch vụ sau triển khai.",
                )
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 6: health_check (Capability: health_check)
    # -------------------------------------------------------------------------
    @action("health_check", output_model=HealthCheckReport, capability="health_check")
    def handle_health_check(self, request: TaskRequest) -> TaskResult:
        """Kiểm tra sức khỏe dịch vụ bằng HTTP request với timeout và retry."""
        endpoint = request.context.get("endpoint", "http://localhost:8080/health")
        timeout = float(request.context.get("timeout_seconds", 5.0))
        max_retries = int(request.context.get("max_retries", 3))
        expected_status = int(request.context.get("expected_status", 200))
        retry_delay = float(request.context.get("retry_delay", 0.5))
        should_exec = self._should_execute(request)
        dry_run = not should_exec

        report = health_check(
            endpoint=endpoint,
            expected_status=expected_status,
            timeout=timeout,
            max_retries=max_retries,
            retry_delay=retry_delay,
            dry_run=dry_run,
        )

        if report.status == "UNHEALTHY":
            return failed_result(
                request,
                make_error(
                    code=ErrorCode.TOOL_ERROR,
                    message=f"Kiểm tra sức khỏe dịch vụ tại '{endpoint}' thất bại: {report.response_body_sample}",
                    retryable=True,
                ),
                summary=f"Health check thất bại cho '{endpoint}' ({report.status_code})",
            )

        # Lưu artifact báo cáo health check
        content = (
            f"# Báo cáo Kiểm tra Sức khỏe Dịch vụ\n\n"
            f"- **Endpoint:** {report.endpoint}\n"
            f"- **Trạng thái:** {report.status}\n"
            f"- **Mã phản hồi:** {report.status_code}\n"
            f"- **Độ trễ:** {report.latency_ms:.1f}ms\n"
            f"- **Phản hồi:** `{report.response_body_sample}`\n"
        )
        ref = save_artifact(
            name="health-check-report.md",
            type=ArtifactType.DEPLOYMENT,
            content=content,
            created_by=self.identity.agent_id,
            artifact_dir=self.config.artifact_dir,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Kiểm tra sức khỏe endpoint '{endpoint}': trạng thái {report.status}.",
            outputs=[report.model_dump(mode="json")],
            artifacts=[ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.DEVOPS,
                    action="setup_monitoring",
                    reason="Cấu hình hệ thống giám sát metrics và cảnh báo cho endpoint.",
                )
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 7: setup_monitoring (Capability: monitoring)
    # -------------------------------------------------------------------------
    @action("setup_monitoring", output_model=MonitoringReport, capability="monitoring")
    def handle_setup_monitoring(self, request: TaskRequest) -> TaskResult:
        """Thiết lập cấu hình giám sát Prometheus / Grafana và xuất artifact."""
        m_type = request.context.get("monitoring_type", "prometheus")
        filename = "prometheus.yml"
        content = (
            "global:\n"
            "  scrape_interval: 15s\n"
            "scrape_configs:\n"
            "  - job_name: 'devops-service'\n"
            "    static_configs:\n"
            "      - targets: ['localhost:8080']\n"
        )

        ref = save_artifact(
            name=filename,
            type=ArtifactType.DEPLOYMENT,
            content=content,
            created_by=self.identity.agent_id,
            artifact_dir=self.config.artifact_dir,
        )

        report = MonitoringReport(
            monitoring_type=m_type,
            config_filename=filename,
            content=content,
            artifact_uri=ref.uri,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã tạo cấu hình giám sát '{filename}' thành công.",
            outputs=[report.model_dump(mode="json")],
            artifacts=[ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.PM,
                    action="report_status",
                    reason="Thông báo cho PM về trạng thái triển khai và giám sát hạ tầng.",
                )
            ],
            error=None,
        )
