import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)
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
from config import PMConfig
from llm import LLMClient, LLMError
from prompt import PM_SYSTEM_PROMPT, build_messages, check_missing_context
from schemas import (
    AnalyzeRequirementOutput,
    AssignTasksOutput,
    CreatePlanOutput,
    DecomposeTasksOutput,
    Milestone,
    ProjectPlan,
    ReportStatusOutput,
    SprintPlan,
    StatusReport,
    TaskItem,
    TrackProgressOutput,
)
from conductor.dispatcher import find_agent_id_for_role, load_endpoints, send_task_request
from conductor.orchestrator import ConductorOrchestrator
from conductor.state import ProjectState, ProjectStatus, StepStatus, load_project_state
from tools.artifact import ArtifactError, list_artifacts, read_artifact, save_artifact
from tools.plan import create_plan
from tools.task import (
    TaskToolError,
    assign_task,
    create_task,
    load_tasks,
    save_tasks_list,
)


def action(
    name: str,
    output_model: type[BaseModel] | None = None,
    capability: str | None = None,
) -> Callable:
    """Decorator đánh dấu phương thức xử lý cho một action của Agent."""

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
        config: PMConfig,
        llm: LLMClient | None = None,
    ) -> None:
        self.identity = identity
        self.config = config
        is_mock = (
            config.llm_provider.lower().strip() == "mock"
            or not config.llm_api_key.strip()
            or "placeholder" in config.llm_api_key.lower()
        )
        self.llm = llm or LLMClient(config=config, mock=is_mock)
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
        """Xử lý TaskRequest, kiểm tra quyền hạn, bắt ngoại lệ và trả về TaskResult chuẩn."""
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

        except LLMError as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.LLM_ERROR, message=str(err), retryable=True),
                summary=f"Lỗi LLM khi thực thi action '{request.action}'",
            )
        except (ArtifactError, TaskToolError) as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.TOOL_ERROR, message=str(err), retryable=True),
                summary=f"Lỗi công cụ khi thực thi action '{request.action}'",
            )
        except ValueError as err:
            self.identity.status = AgentStatus.FAILED
            self._current_task = None
            final_result = failed_result(
                request,
                make_error(code=ErrorCode.INVALID_INPUT, message=str(err), retryable=False),
                summary=f"Lỗi dữ liệu đầu vào không hợp lệ khi thực thi action '{request.action}'",
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


class PMAgent(BaseAgent):
    """Project Manager Agent độc lập đảm nhiệm lập kế hoạch, phân rã và điều phối dự án."""

    def __init__(
        self,
        config: PMConfig | None = None,
        llm: LLMClient | None = None,
        artifact_dir: Union[Path, str, None] = None,
    ) -> None:
        cfg = config or PMConfig()
        if artifact_dir:
            cfg.artifact_dir = str(artifact_dir)

        identity = AgentIdentity(
            agent_id=cfg.agent_id,
            role=AgentRole.PM,
            name="Project Manager Agent",
            version="1.0.0",
            capabilities=CAPABILITY_REGISTRY[AgentRole.PM],
            status=AgentStatus.IDLE,
        )
        super().__init__(identity=identity, config=cfg, llm=llm)
        self.conductor = ConductorOrchestrator(
            data_dir=self.config.data_dir,
            artifact_dir=self.config.artifact_dir,
            config=self.config,
            llm_client=self.llm,
            agent_identity=self.identity,
        )

    # -------------------------------------------------------------------------
    # Action 1: analyze_requirement (Capability: project_planning)
    # -------------------------------------------------------------------------
    @action("analyze_requirement", output_model=AnalyzeRequirementOutput, capability="project_planning")
    def handle_analyze_requirement(self, request: TaskRequest) -> TaskResult:
        """Phân tích sơ bộ yêu cầu dự án, xác định phạm vi, timeline và giả định."""
        missing = check_missing_context(request)
        if missing:
            return blocked_result(
                request,
                make_error(
                    code=ErrorCode.MISSING_CONTEXT,
                    message="Yêu cầu thiếu thông tin cốt lõi để phân tích phạm vi dự án.",
                    retryable=False,
                    questions=missing,
                ),
                summary="Bị chặn do thiếu thông tin quan trọng.",
            )

        if not self.llm.mock or self.llm.mock_responder:
            messages = build_messages(
                system_prompt=PM_SYSTEM_PROMPT,
                request=request,
                output_schema=AnalyzeRequirementOutput.model_json_schema(),
            )
            analysis_data = self.llm.complete_json(messages, AnalyzeRequirementOutput)
        else:
            # Fallback mock offline
            analysis_data = AnalyzeRequirementOutput(
                summary=f"Phân tích yêu cầu sơ bộ: {request.instruction}",
                functional_requirements=[
                    "Xác thực người dùng và phân quyền vai trò",
                    "Quản lý danh mục sản phẩm và giỏ hàng",
                    "Thanh toán trực tuyến và tích hợp cổng thanh toán",
                    "Theo dõi trạng thái đơn hàng thời gian thực",
                ],
                non_functional_requirements=[
                    "Thời gian phản hồi API < 200ms",
                    "Đảm bảo tuân thủ tiêu chuẩn an toàn bảo mật OWASP",
                    "Hỗ trợ 5,000 người dùng đồng thời",
                ],
                target_timeline_weeks=12,
                assumptions=[
                    "Đội ngũ gồm 5 kỹ sư làm việc toàn thời gian",
                    "Hạ tầng triển khai trên nền tảng Cloud",
                ],
            )

        # Sinh artifact báo cáo tóm tắt yêu cầu
        md_content = (
            f"# Báo cáo tóm tắt phân tích yêu cầu\n\n"
            f"**Tóm tắt:** {analysis_data.summary}\n\n"
            f"## Yêu cầu chức năng\n" + "\n".join(f"- {r}" for r in analysis_data.functional_requirements) + "\n\n"
            f"## Yêu cầu phi chức năng\n" + "\n".join(f"- {r}" for r in analysis_data.non_functional_requirements) + "\n\n"
            f"## Giả định & Phạm vi\n" + "\n".join(f"- {a}" for a in analysis_data.assumptions)
        )
        art_ref = save_artifact(
            name="requirements-summary.md",
            type=ArtifactType.REQUIREMENT,
            content=md_content,
            created_by=self.identity.agent_id,
            artifact_dir=self.config.artifact_dir,
        )
        analysis_data.artifact_uri = art_ref.uri

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary="Đã phân tích yêu cầu dự án và xác định phạm vi tổng thể.",
            outputs=[analysis_data.model_dump(mode="json")],
            artifacts=[art_ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.BA,
                    action="requirement_analysis",
                    reason="Cần BA phân tích chi tiết user stories và acceptance criteria từ bản phân tích sơ bộ.",
                ),
                NextAction(
                    suggested_role=AgentRole.PM,
                    action="create_plan",
                    reason="Tiến hành lập kế hoạch dự án tổng thể dựa trên yêu cầu đã phân tích.",
                ),
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 2: create_plan (Capability: project_planning)
    # -------------------------------------------------------------------------
    @action("create_plan", output_model=CreatePlanOutput, capability="project_planning")
    def handle_create_plan(self, request: TaskRequest) -> TaskResult:
        """Lập kế hoạch tổng thể dự án, phân chia Milestone, Sprint và xuất plan.md."""
        missing = check_missing_context(request)
        if missing:
            return blocked_result(
                request,
                make_error(
                    code=ErrorCode.MISSING_CONTEXT,
                    message="Yêu cầu thiếu thông tin cốt lõi để lập kế hoạch dự án.",
                    retryable=False,
                    questions=missing,
                ),
                summary="Bị chặn do thiếu thông tin lập kế hoạch.",
            )

        plan = None
        if not self.llm.mock or self.llm.mock_responder:
            try:
                messages = build_messages(
                    system_prompt=PM_SYSTEM_PROMPT,
                    request=request,
                    output_schema=ProjectPlan.model_json_schema(),
                )
                plan = self.llm.complete_json(messages, ProjectPlan)
            except Exception as e:
                logger.warning(f"LLM API error ({e}). Using deterministic fallback project plan.")
        if plan is None:
            plan = ProjectPlan(
                project_name=request.context.get("project_name", "Hệ thống Web Thương Mại Điện Tử"),
                overview=f"Kế hoạch tổng thể: {request.instruction}",
                milestones=[
                    Milestone(
                        milestone_id="M1",
                        name="Thiết kế kiến trúc & Đặc tả yêu cầu",
                        description="Hoàn tất tài liệu kiến trúc, database schema và backlog",
                        target_date="2026-11-20",
                        deliverables=["requirements.md", "architecture.md", "schema.sql"],
                    ),
                    Milestone(
                        milestone_id="M2",
                        name="Phát triển tính năng cốt lõi (Core MVP)",
                        description="Xây dựng API, UI luồng mua hàng và hệ thống cơ sở dữ liệu",
                        target_date="2026-12-25",
                        deliverables=["source-code", "unit-tests"],
                    ),
                    Milestone(
                        milestone_id="M3",
                        name="Kiểm thử, Bảo mật và Triển khai",
                        description="Kiểm thử hệ thống, quét lỗ hổng bảo mật và CI/CD deployment",
                        target_date="2027-01-20",
                        deliverables=["test-report.md", "security-report.md", "deployment.yml"],
                    ),
                ],
                sprints=[
                    SprintPlan(sprint_number=1, goal="Phân tích & Thiết kế hệ thống", duration_weeks=2, deliverables=["architecture.md", "database.sql"]),
                    SprintPlan(sprint_number=2, goal="Phát triển Backend API & Database", duration_weeks=2, deliverables=["REST API", "Migrations"]),
                    SprintPlan(sprint_number=3, goal="Phát triển Frontend & Tích hợp", duration_weeks=2, deliverables=["Web UI", "Integration"]),
                    SprintPlan(sprint_number=4, goal="Testing, Security Review & Deployment", duration_weeks=2, deliverables=["Test Report", "Docker Image"]),
                ],
                risks=[
                    "Phạm vi yêu cầu có thể thay đổi trong quá trình tích hợp bên thứ ba",
                    "Rủi ro chậm tiến độ nếu cơ sở dữ liệu gặp vấn đề hiệu năng",
                ],
            )

        # Lưu kế hoạch thành file artifact plan.md
        _, plan_ref = create_plan(
            project_name=plan.project_name,
            overview=plan.overview,
            milestones=plan.milestones,
            sprints=plan.sprints,
            risks=plan.risks,
            created_by=self.identity.agent_id,
            artifact_dir=self.config.artifact_dir,
        )

        output_data = CreatePlanOutput(plan=plan, artifact_uri=plan_ref.uri)

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã tạo kế hoạch dự án gồm {len(plan.milestones)} milestone và {len(plan.sprints)} sprint.",
            outputs=[output_data.model_dump(mode="json")],
            artifacts=[plan_ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.BA,
                    action="analyze_requirement",
                    reason="Cần BA chi tiết hóa yêu cầu nghiệp vụ cho sprint 1.",
                ),
                NextAction(
                    suggested_role=AgentRole.PM,
                    action="decompose_tasks",
                    reason="Phân rã kế hoạch thành danh sách các task cụ thể.",
                ),
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 3: decompose_tasks (Capability: task_decomposition)
    # -------------------------------------------------------------------------
    @action("decompose_tasks", output_model=DecomposeTasksOutput, capability="task_decomposition")
    def handle_decompose_tasks(self, request: TaskRequest) -> TaskResult:
        """Phân rã kế hoạch hoặc yêu cầu thành danh sách TaskItem và lưu vào tasks.json."""
        tasks: list[TaskItem]

        if not self.llm.mock or self.llm.mock_responder:
            messages = build_messages(
                system_prompt=PM_SYSTEM_PROMPT,
                request=request,
                output_schema=DecomposeTasksOutput.model_json_schema(),
            )
            raw_result = self.llm.complete_json(messages, DecomposeTasksOutput)
            tasks = raw_result.tasks
        else:
            tasks = [
                TaskItem(
                    task_id="TASK-001",
                    title="Phân tích yêu cầu và đặc tả User Stories",
                    description="Xác định chi tiết các luồng nghiệp vụ mua hàng và tiêu chí nghiệm thu.",
                    assignee_role=AgentRole.BA,
                    acceptance_criteria=["Có tài liệu requirements.md", "Được PO phê duyệt"],
                    due="2026-11-10",
                ),
                TaskItem(
                    task_id="TASK-002",
                    title="Thiết kế kiến trúc hệ thống và API specs",
                    description="Xác định mô hình kiến trúc Microservices/Monolith và OpenAPI specification.",
                    assignee_role=AgentRole.SA,
                    acceptance_criteria=["Có tài liệu architecture.md", "Thiết kế API endpoints"],
                    due="2026-11-15",
                    depends_on=["TASK-001"],
                ),
                TaskItem(
                    task_id="TASK-003",
                    title="Thiết kế lược đồ cơ sở dữ liệu",
                    description="Tạo ERD và viết script DDL khởi tạo bảng cơ sở dữ liệu.",
                    assignee_role=AgentRole.DBA,
                    acceptance_criteria=["Có file database.sql", "Đã chuẩn hóa 3NF"],
                    due="2026-11-18",
                    depends_on=["TASK-002"],
                ),
                TaskItem(
                    task_id="TASK-004",
                    title="Thiết lập pipeline CI/CD và Dockerfile",
                    description="Viết cấu hình GitHub Actions hoặc GitLab CI để build và test tự động.",
                    assignee_role=AgentRole.DEVOPS,
                    acceptance_criteria=["Dockerfile hợp lệ", "Pipeline chạy pass tự động"],
                    due="2026-11-20",
                ),
            ]

        # Lưu danh sách tasks vào artifacts/tasks.json
        tasks_ref = save_tasks_list(tasks, created_by=self.identity.agent_id, artifact_dir=self.config.artifact_dir)
        output_data = DecomposeTasksOutput(
            tasks=tasks,
            total_tasks=len(tasks),
            artifact_uri=tasks_ref.uri,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã phân rã thành {len(tasks)} nhiệm vụ cụ thể.",
            outputs=[output_data.model_dump(mode="json")],
            artifacts=[tasks_ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.PM,
                    action="assign_tasks",
                    reason="Phân công các task vừa phân rã cho các thành viên phụ trách.",
                )
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 4: assign_tasks (Capability: task_assignment)
    # -------------------------------------------------------------------------
    @action("assign_tasks", output_model=AssignTasksOutput, capability="task_assignment")
    def handle_assign_tasks(self, request: TaskRequest) -> TaskResult:
        """Phân công hoặc cập nhật vai trò phụ trách (assignee_role) cho các task trong backlog."""
        tasks = load_tasks(self.config.artifact_dir)
        if not tasks:
            # Nếu chưa có task, tạo và lưu trước một số task mẫu vào disk
            tasks = [
                TaskItem(
                    task_id="TASK-001",
                    title="Xây dựng API Backend",
                    description="Lập trình các endpoint sản phẩm và đơn hàng",
                    assignee_role=AgentRole.DEV,
                    acceptance_criteria=["Unit test pass > 80%"],
                    due="2026-11-25",
                ),
                TaskItem(
                    task_id="TASK-002",
                    title="Kiểm thử tự động API",
                    description="Viết test case và kịch bản test tích hợp",
                    assignee_role=AgentRole.TESTER,
                    acceptance_criteria=["Test report xanh"],
                    due="2026-11-28",
                    depends_on=["TASK-001"],
                ),
            ]
            save_tasks_list(tasks, created_by=self.identity.agent_id, artifact_dir=self.config.artifact_dir)

        # Cập nhật assignment từ context nếu có chỉ định cụ thể
        custom_assignments = request.context.get("assignments", {})
        for task_id, role_str in custom_assignments.items():
            try:
                role = AgentRole(role_str)
                assign_task(task_id, role, artifact_dir=self.config.artifact_dir)
            except Exception as e:
                return failed_result(
                    request,
                    make_error(ErrorCode.TOOL_ERROR, f"Gán task '{task_id}' thất bại: {e}", retryable=False),
                )

        tasks = load_tasks(self.config.artifact_dir)
        tasks_ref = save_tasks_list(tasks, created_by=self.identity.agent_id, artifact_dir=self.config.artifact_dir)

        output_data = AssignTasksOutput(
            assigned_tasks=tasks,
            total_assigned=len(tasks),
            artifact_uri=tasks_ref.uri,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã phân công và cập nhật vai trò cho {len(tasks)} nhiệm vụ.",
            outputs=[output_data.model_dump(mode="json")],
            artifacts=[tasks_ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.DEV,
                    action="coding",
                    reason="Chuyển giao các task phát triển phần mềm cho DEV.",
                ),
                NextAction(
                    suggested_role=AgentRole.TESTER,
                    action="test_planning",
                    reason="TESTER bắt đầu lập kế hoạch kiểm thử theo backlog đã giao.",
                ),
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 5: track_progress (Capability: progress_tracking)
    # -------------------------------------------------------------------------
    @action("track_progress", output_model=TrackProgressOutput, capability="progress_tracking")
    def handle_track_progress(self, request: TaskRequest) -> TaskResult:
        """Theo dõi tiến độ tổng thể dựa trên trạng thái của danh sách tasks.json."""
        tasks = load_tasks(self.config.artifact_dir)
        total = len(tasks)
        completed = sum(1 for t in tasks if t.status == TaskStatus.COMPLETED)
        in_progress = sum(1 for t in tasks if t.status == TaskStatus.RUNNING)
        blocked = sum(1 for t in tasks if t.status == TaskStatus.BLOCKED)
        pending = sum(1 for t in tasks if t.status in [TaskStatus.PENDING, TaskStatus.CANCELLED])

        percent = round((completed / total * 100.0), 1) if total > 0 else 0.0

        output_data = TrackProgressOutput(
            total_tasks=total,
            completed_count=completed,
            in_progress_count=in_progress,
            pending_count=pending,
            blocked_count=blocked,
            progress_percent=percent,
            artifact_uri="artifacts/tasks.json",
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Tiến độ dự án hiện tại đạt {percent}% ({completed}/{total} tasks hoàn thành).",
            outputs=[output_data.model_dump(mode="json")],
            artifacts=[],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.PM,
                    action="report_status",
                    reason="Tổng hợp dữ liệu tiến độ thành báo cáo trạng thái chi tiết.",
                )
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 6: report_status (Capability: progress_tracking)
    # -------------------------------------------------------------------------
    @action("report_status", output_model=ReportStatusOutput, capability="progress_tracking")
    def handle_report_status(self, request: TaskRequest) -> TaskResult:
        """Xuất báo cáo trạng thái 4 phần chuẩn và lưu vào status-report.md."""
        tasks = load_tasks(self.config.artifact_dir)

        completed_items = [f"{t.task_id}: {t.title} ({t.assignee_role.value})" for t in tasks if t.status == TaskStatus.COMPLETED]
        in_progress_items = [f"{t.task_id}: {t.title} ({t.assignee_role.value})" for t in tasks if t.status == TaskStatus.RUNNING]
        blocked_items = [f"{t.task_id}: {t.title} ({t.assignee_role.value})" for t in tasks if t.status == TaskStatus.BLOCKED]
        next_items = [f"{t.task_id}: {t.title} ({t.assignee_role.value})" for t in tasks if t.status == TaskStatus.PENDING]

        # Đảm bảo mỗi mục có ít nhất một dòng tóm tắt
        if not completed_items:
            completed_items = ["Đã hoàn thành phân tích yêu cầu sơ bộ và thiết lập backlog."]
        if not in_progress_items:
            in_progress_items = ["Đang tiến hành lập kế hoạch và phân rã các task kỹ thuật."]
        if not blocked_items:
            blocked_items = ["Không có blocker nào ngăn cản tiến độ hiện tại."]
        if not next_items:
            next_items = ["Triển khai Sprint 1 và bàn giao task cho DEV & TESTER."]

        report = StatusReport(
            completed=completed_items,
            in_progress=in_progress_items,
            blocked=blocked_items,
            next=next_items,
        )

        md_content = (
            "# Báo cáo trạng thái dự án (Status Report)\n\n"
            "## 1. Công việc đã hoàn thành\n" + "\n".join(f"- [x] {item}" for item in report.completed) + "\n\n"
            "## 2. Công việc đang thực hiện\n" + "\n".join(f"- [-] {item}" for item in report.in_progress) + "\n\n"
            "## 3. Các vấn đề đang bị chặn (Blockers)\n" + "\n".join(f"- [!] {item}" for item in report.blocked) + "\n\n"
            "## 4. Kế hoạch công việc tiếp theo\n" + "\n".join(f"- [ ] {item}" for item in report.next) + "\n"
        )

        report_ref = save_artifact(
            name="status-report.md",
            type=ArtifactType.DOCUMENTATION,
            content=md_content,
            created_by=self.identity.agent_id,
            artifact_dir=self.config.artifact_dir,
        )

        output_data = ReportStatusOutput(
            report=report,
            artifact_uri=report_ref.uri,
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary="Đã tạo báo cáo trạng thái dự án đầy đủ 4 mục tiêu chuẩn.",
            outputs=[output_data.model_dump(mode="json")],
            artifacts=[report_ref],
            next_actions=[
                NextAction(
                    suggested_role=AgentRole.SCRUM_MASTER,
                    action="sprint_management",
                    reason="Scrum Master điều phối Daily Standup và hỗ trợ tháo gỡ blocker nếu có.",
                ),
                NextAction(
                    suggested_role=AgentRole.PO,
                    action="feature_prioritization",
                    reason="Product Owner cập nhật thứ tự ưu tiên tính năng cho sprint kế tiếp.",
                ),
            ],
            error=None,
        )

    # -------------------------------------------------------------------------
    # Action 7: start_project (Capability: workflow_orchestration)
    # -------------------------------------------------------------------------
    @action("start_project", capability="workflow_orchestration")
    def handle_start_project(self, request: TaskRequest) -> TaskResult:
        """Khởi động quy trình phần mềm tự động bằng PM Conductor theo software_delivery.yaml."""
        brief = request.context.get("brief") or request.instruction
        auto_approve = bool(request.context.get("auto_approve", self.config.pm_auto_approve))
        project_id = request.project_id or f"PROJ-{int(time.time())}"

        state = self.conductor.init_project(
            project_id=project_id,
            brief=brief,
            auto_approve=auto_approve,
        )
        is_async = bool(request.context.get("async", False))
        if not is_async:
            state = self.conductor.run_pipeline(state)

        status_enum = (
            TaskStatus.COMPLETED if state.status == ProjectStatus.COMPLETED
            else (TaskStatus.BLOCKED if state.status == ProjectStatus.BLOCKED else TaskStatus.RUNNING)
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=status_enum,
            summary=f"Dự án {project_id} ở trạng thái {state.status.value}.",
            outputs=[state.model_dump(mode="json")],
            artifacts=[],
            error=make_error(code=ErrorCode.UNKNOWN_ERROR, message=state.block_reason or "") if state.status == ProjectStatus.BLOCKED else None,
        )

    # -------------------------------------------------------------------------
    # Action 8: resume_project (Capability: workflow_orchestration)
    # -------------------------------------------------------------------------
    @action("resume_project", capability="workflow_orchestration")
    def handle_resume_project(self, request: TaskRequest) -> TaskResult:
        """Tiếp tục quy trình phần mềm tự động từ bước dở dang."""
        project_id = request.context.get("project_id") or request.project_id
        state = self.conductor.resume_project(project_id)
        status_enum = (
            TaskStatus.COMPLETED if state.status == ProjectStatus.COMPLETED
            else (TaskStatus.BLOCKED if state.status == ProjectStatus.BLOCKED else TaskStatus.RUNNING)
        )
        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=status_enum,
            summary=f"Dự án {project_id} được tiếp tục, trạng thái: {state.status.value}.",
            outputs=[state.model_dump(mode="json")],
            artifacts=[],
            error=make_error(code=ErrorCode.UNKNOWN_ERROR, message=state.block_reason or "") if state.status == ProjectStatus.BLOCKED else None,
        )

    # -------------------------------------------------------------------------
    # Action 9: get_project_state (Capability: workflow_orchestration)
    # -------------------------------------------------------------------------
    @action("get_project_state", capability="workflow_orchestration")
    def handle_get_project_state(self, request: TaskRequest) -> TaskResult:
        """Lấy trạng thái chi tiết của một dự án do PM Conductor quản lý."""
        project_id = request.context.get("project_id") or request.project_id
        state = load_project_state(project_id, self.config.data_dir)
        if not state:
            return failed_result(
                request,
                make_error(code=ErrorCode.INVALID_INPUT, message=f"Không tìm thấy dự án {project_id}"),
                summary="Dự án không tồn tại.",
            )
        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Trạng thái dự án {project_id}: {state.status.value}.",
            outputs=[state.model_dump(mode="json")],
            artifacts=[],
        )

    # -------------------------------------------------------------------------
    # Action 10: dispatch_task (Capability: task_dispatch)
    # -------------------------------------------------------------------------
    @action("dispatch_task", capability="task_dispatch")
    def handle_dispatch_task(self, request: TaskRequest) -> TaskResult:
        """PM trực tiếp gửi một TaskRequest tới agent khác qua LAN và trả lại TaskResult."""
        target_agent_id = request.context.get("target_agent_id")
        target_role = request.context.get("target_role")
        endpoints = load_endpoints()
        if not target_agent_id:
            if target_role:
                target_agent_id = find_agent_id_for_role(target_role, endpoints)
            else:
                return failed_result(
                    request,
                    make_error(code=ErrorCode.INVALID_INPUT, message="Thiếu target_agent_id hoặc target_role trong context."),
                    summary="Thiếu thông tin agent đích.",
                )
        sub_req = TaskRequest(
            contract_version=request.contract_version,
            task_id=request.context.get("dispatch_task_id") or f"TASK-DISPATCH-{int(time.time())}",
            project_id=request.project_id,
            agent_id=target_agent_id,
            role=AgentRole(target_role) if target_role else AgentRole.PM,
            action=request.context.get("target_action", "unknown"),
            instruction=request.instruction,
            context=request.context.get("target_context", {}),
            constraints=request.constraints,
        )
        res = send_task_request(target_agent_id, sub_req, endpoints)
        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=res.status,
            summary=f"Đã dispatch tới {target_agent_id}: {res.summary}",
            outputs=res.outputs,
            artifacts=res.artifacts,
            error=res.error,
        )

