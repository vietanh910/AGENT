from typing import Any
from pydantic import BaseModel, Field

from agent_contract.enums import AgentRole, TaskStatus
from agent_contract.project_plan import ModulePlan, ChangePlan


class Milestone(BaseModel):
    """Cột mốc quan trọng trong kế hoạch dự án."""

    milestone_id: str
    name: str
    description: str
    target_date: str | None = None
    deliverables: list[str] = Field(default_factory=list)


class SprintPlan(BaseModel):
    """Kế hoạch chi tiết theo từng Sprint."""

    sprint_number: int
    goal: str
    duration_weeks: int = 2
    deliverables: list[str] = Field(default_factory=list)


class ProjectPlan(BaseModel):
    """Kế hoạch tổng thể dự án gồm các milestone, sprint và rủi ro."""

    project_name: str
    overview: str
    milestones: list[Milestone] = Field(default_factory=list)
    sprints: list[SprintPlan] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class TaskItem(BaseModel):
    """Một nhiệm vụ trong danh sách backlog."""

    task_id: str
    title: str
    description: str
    assignee_role: AgentRole
    acceptance_criteria: list[str] = Field(default_factory=list)
    due: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING


class StatusReport(BaseModel):
    """Báo cáo trạng thái 4 phần chuẩn: đã xong, đang làm, bị chặn, việc tiếp theo."""

    completed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    blocked: list[str] = Field(default_factory=list)
    next: list[str] = Field(default_factory=list)


# --- Output models riêng cho từng action của PM ---


class AnalyzeRequirementOutput(BaseModel):
    """Đầu ra của action analyze_requirement."""

    summary: str
    functional_requirements: list[str] = Field(default_factory=list)
    non_functional_requirements: list[str] = Field(default_factory=list)
    target_timeline_weeks: int = 12
    assumptions: list[str] = Field(default_factory=list)
    artifact_uri: str | None = None


class CreatePlanOutput(BaseModel):
    """Đầu ra của action create_plan."""

    plan: ProjectPlan | None = None
    module_plan: ModulePlan | ChangePlan | None = None
    planning_warning: str | None = None
    artifact_uri: str


class DecomposeTasksOutput(BaseModel):
    """Đầu ra của action decompose_tasks."""

    tasks: list[TaskItem]
    total_tasks: int
    artifact_uri: str


class AssignTasksOutput(BaseModel):
    """Đầu ra của action assign_tasks."""

    assigned_tasks: list[TaskItem]
    total_assigned: int
    artifact_uri: str


class TrackProgressOutput(BaseModel):
    """Đầu ra của action track_progress."""

    total_tasks: int
    completed_count: int
    in_progress_count: int
    pending_count: int
    blocked_count: int
    progress_percent: float
    artifact_uri: str | None = None


class ReportStatusOutput(BaseModel):
    """Đầu ra của action report_status."""

    report: StatusReport
    artifact_uri: str
