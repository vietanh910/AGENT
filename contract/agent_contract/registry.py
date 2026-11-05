from typing import Any

from agent_contract.enums import AgentRole, ArtifactType

CAPABILITY_REGISTRY: dict[AgentRole, list[str]] = {
    AgentRole.PM: [
        "project_planning",
        "task_decomposition",
        "task_assignment",
        "progress_tracking",
        "task_dispatch",
        "workflow_orchestration",
    ],
    AgentRole.BA: [
        "requirement_analysis",
        "user_story",
        "use_case",
        "acceptance_criteria",
    ],
    AgentRole.SA: [
        "system_architecture",
        "api_design",
        "technology_selection",
    ],
    AgentRole.DBA: [
        "database_design",
        "schema_design",
        "sql",
        "query_optimization",
    ],
    AgentRole.DEV: [
        "coding",
        "refactoring",
        "bug_fixing",
        "code_review",
    ],
    AgentRole.TESTER: [
        "test_planning",
        "test_case_generation",
        "automated_testing",
        "bug_reporting",
    ],
    AgentRole.DEVOPS: [
        "git",
        "docker",
        "ci_cd",
        "deployment",
        "monitoring",
        "health_check",
    ],
    AgentRole.SECURITY: [
        "security_analysis",
        "vulnerability_scan",
        "dependency_scan",
        "security_review",
    ],
    AgentRole.PO: [
        "product_vision",
        "feature_prioritization",
        "backlog_management",
        "acceptance",
    ],
    AgentRole.SCRUM_MASTER: [
        "sprint_management",
        "scrum_management",
        "blocker_tracking",
        "retrospective",
    ],
}

ACTION_MAP: dict[AgentRole, dict[str, str]] = {
    AgentRole.PM: {
        "analyze_requirement": "project_planning",
        "create_plan": "project_planning",
        "decompose_tasks": "task_decomposition",
        "assign_tasks": "task_assignment",
        "track_progress": "progress_tracking",
        "report_status": "progress_tracking",
        "start_project": "workflow_orchestration",
        "resume_project": "workflow_orchestration",
        "get_project_state": "workflow_orchestration",
        "dispatch_task": "task_dispatch",
        "recover_task": "workflow_orchestration",
    },
    AgentRole.DEVOPS: {
        "git_operation": "git",
        "build_image": "docker",
        "generate_ci_config": "ci_cd",
        "run_tests": "ci_cd",
        "deploy": "deployment",
        "health_check": "health_check",
        "setup_monitoring": "monitoring",
    },
    AgentRole.PO: {
        "define_backlog": "backlog_management",
        "accept_release": "acceptance",
    },
    AgentRole.BA: {
        "write_requirements": "requirement_analysis",
        "write_user_stories": "user_story",
    },
    AgentRole.SA: {
        "design_architecture": "system_architecture",
    },
    AgentRole.DBA: {
        "design_schema": "schema_design",
    },
    AgentRole.DEV: {
        "implement_project": "coding",
        "implement_feature": "coding",
        "fix_bugs": "bug_fixing",
    },
    AgentRole.TESTER: {
        "create_test_cases": "test_case_generation",
        "run_test_suite": "automated_testing",
    },
    AgentRole.SECURITY: {
        "security_review": "security_review",
    },
    AgentRole.SCRUM_MASTER: {
        "track_blockers": "blocker_tracking",
        "run_sprint_review": "sprint_management",
        "run_retrospective": "retrospective",
    },
}

# =============================================================================
# BẢNG ĐĂNG KÝ AGENT MẶC ĐỊNH (System Phase - 10 Agent / 5 Máy)
# =============================================================================
AGENT_REGISTRY_DEFAULT: list[dict[str, Any]] = [
    {
        "agent_id": "pm-01",
        "role": AgentRole.PM,
        "machine": "PC1",
        "runner": "LocalWorker",
    },
    {
        "agent_id": "devops-01",
        "role": AgentRole.DEVOPS,
        "machine": "PC1",
        "runner": "LocalWorker",
    },
    {
        "agent_id": "sa-01",
        "role": AgentRole.SA,
        "machine": "PC2",
        "runner": "Worker Host",
    },
    {
        "agent_id": "dba-01",
        "role": AgentRole.DBA,
        "machine": "PC2",
        "runner": "Worker Host",
    },
    {
        "agent_id": "ba-01",
        "role": AgentRole.BA,
        "machine": "PC3",
        "runner": "Worker Host",
    },
    {
        "agent_id": "po-01",
        "role": AgentRole.PO,
        "machine": "PC3",
        "runner": "Worker Host",
    },
    {
        "agent_id": "dev-01",
        "role": AgentRole.DEV,
        "machine": "PC4",
        "runner": "Worker Host",
    },
    {
        "agent_id": "tester-01",
        "role": AgentRole.TESTER,
        "machine": "PC4",
        "runner": "Worker Host",
    },
    {
        "agent_id": "scrum_master-01",
        "role": AgentRole.SCRUM_MASTER,
        "machine": "PC5",
        "runner": "Worker Host",
    },
    {
        "agent_id": "security-01",
        "role": AgentRole.SECURITY,
        "machine": "PC5",
        "runner": "Worker Host",
    },
]

# Ánh xạ artifact mặc định tương ứng với từng pipeline action
PIPELINE_ACTION_ARTIFACT_MAP: dict[tuple[AgentRole, str], ArtifactType] = {
    (AgentRole.PO, "define_backlog"): ArtifactType.BACKLOG,
    (AgentRole.PO, "accept_release"): ArtifactType.DOCUMENTATION,
    (AgentRole.BA, "write_requirements"): ArtifactType.REQUIREMENT,
    (AgentRole.BA, "write_user_stories"): ArtifactType.USER_STORY,
    (AgentRole.SA, "design_architecture"): ArtifactType.ARCHITECTURE,
    (AgentRole.DBA, "design_schema"): ArtifactType.DATABASE_SCHEMA,
    (AgentRole.DEV, "implement_project"): ArtifactType.SOURCE_CODE,
    (AgentRole.DEV, "implement_feature"): ArtifactType.SOURCE_CODE,
    (AgentRole.DEV, "fix_bugs"): ArtifactType.SOURCE_CODE,
    (AgentRole.TESTER, "create_test_cases"): ArtifactType.TEST_CASE,
    (AgentRole.TESTER, "run_test_suite"): ArtifactType.TEST_REPORT,
    (AgentRole.SECURITY, "security_review"): ArtifactType.SECURITY_REPORT,
    (AgentRole.DEVOPS, "generate_ci_config"): ArtifactType.DEPLOYMENT,
    (AgentRole.DEVOPS, "build_image"): ArtifactType.DEPLOYMENT,
    (AgentRole.DEVOPS, "deploy"): ArtifactType.DEPLOYMENT,
    (AgentRole.DEVOPS, "health_check"): ArtifactType.DEPLOYMENT,
    (AgentRole.SCRUM_MASTER, "track_blockers"): ArtifactType.DOCUMENTATION,
    (AgentRole.SCRUM_MASTER, "run_sprint_review"): ArtifactType.DOCUMENTATION,
    (AgentRole.SCRUM_MASTER, "run_retrospective"): ArtifactType.DOCUMENTATION,
    (AgentRole.PM, "create_plan"): ArtifactType.PLAN,
}
