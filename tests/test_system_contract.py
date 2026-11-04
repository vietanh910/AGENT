"""test_system_contract.py - Bộ kiểm thử Contract mở rộng cho System Phase (v1.2.0).

Kiểm thử:
1. Ví dụ JSON hợp lệ và không hợp lệ cho từng model trong system_models.py và Verdict.
2. Mọi action trong ACTION_MAP có capability thuộc đúng role trong CAPABILITY_REGISTRY.
3. Ánh xạ trạng thái DISPATCH_TO_TASK_STATUS bao phủ đầy đủ tất cả giá trị DispatchState.
4. Validation ArtifactRef.uri cho các dạng hợp lệ (github://, artifact://, tương đối) và không hợp lệ (.., tuyệt đối, sai scheme).
5. AGENT_REGISTRY_DEFAULT gồm đủ 10 agent phân bổ đúng 5 máy theo mục 3 SYSTEM_PLAN.md.
6. Validation agent_id theo định dạng <role viết thường>-NN với role thuộc AgentRole.
"""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from agent_contract.enums import AgentRole, AgentStatus, ErrorCode, TaskStatus
from agent_contract.models import (
    CONTRACT_VERSION,
    ArtifactRef,
    FindingSeverity,
    TaskRequest,
    Verdict,
    VerdictFinding,
    VerdictStatus,
)
from agent_contract.registry import (
    ACTION_MAP,
    AGENT_REGISTRY_DEFAULT,
    CAPABILITY_REGISTRY,
    PIPELINE_ACTION_ARTIFACT_MAP,
)
from agent_contract.system_models import (
    DISPATCH_TO_TASK_STATUS,
    Approval,
    AuditEvent,
    DispatchAck,
    DispatchRequest,
    DispatchState,
    Heartbeat,
    HeartbeatAgentStatus,
    RunStatus,
    RunTask,
    SystemEventType,
    WorkerRegistration,
    WorkflowRun,
)
from agent_contract.validators import validate_agent_id, validate_artifact_uri


# =============================================================================
# 1. KIỂM THỬ MODEL: JSON HỢP LỆ VÀ KHÔNG HỢP LỆ
# =============================================================================

def test_worker_registration_valid_and_invalid():
    """WorkerRegistration nhận JSON hợp lệ và từ chối khi thiếu trường."""
    valid_data = {
        "worker_id": "worker-pc2",
        "machine_id": "PC2",
        "base_url": "http://192.168.1.20:8000",
        "agents": [
            {
                "contract_version": CONTRACT_VERSION,
                "agent_id": "sa-01",
                "role": "SA",
                "name": "SA Agent",
                "version": "1.0.0",
                "capabilities": ["system_architecture", "api_design"],
            }
        ],
    }
    reg = WorkerRegistration.model_validate(valid_data)
    assert reg.worker_id == "worker-pc2"
    assert len(reg.agents) == 1
    assert reg.contract_version == CONTRACT_VERSION

    # Không hợp lệ: Thiếu base_url
    invalid_data = valid_data.copy()
    invalid_data.pop("base_url")
    with pytest.raises(ValidationError):
        WorkerRegistration.model_validate(invalid_data)


def test_heartbeat_valid_and_invalid():
    """Heartbeat nhận JSON hợp lệ và từ chối khi agent_id sai định dạng."""
    valid_data = {
        "worker_id": "worker-pc3",
        "agents": [
            {"agent_id": "ba-01", "status": "IDLE", "current_task": None},
            {"agent_id": "po-01", "status": "RUNNING", "current_task": "TASK-100"},
        ],
        "running_tasks": ["TASK-100"],
    }
    hb = Heartbeat.model_validate(valid_data)
    assert hb.worker_id == "worker-pc3"
    assert len(hb.agents) == 2
    assert hb.agents[1].current_task == "TASK-100"
    assert hb.sent_at.tzinfo is not None

    # Không hợp lệ: agent_id sai định dạng role (unknown-01)
    invalid_data = {
        "worker_id": "worker-pc3",
        "agents": [{"agent_id": "unknown-01", "status": "IDLE"}],
    }
    with pytest.raises(ValidationError):
        Heartbeat.model_validate(invalid_data)


def test_dispatch_request_and_ack_valid_and_invalid():
    """DispatchRequest và DispatchAck xử lý đúng các trường mở rộng."""
    req_data = {
        "task_id": "TASK-001",
        "project_id": "PROJ-1",
        "agent_id": "dev-01",
        "role": "DEV",
        "action": "implement_project",
        "instruction": "Implement code",
        "callback_url": "http://192.168.1.10:8000/api/v1/tasks/TASK-001/result",
        "attempt": 1,
    }
    dispatch = DispatchRequest.model_validate(req_data)
    assert dispatch.callback_url.startswith("http")
    assert dispatch.attempt == 1

    # Không hợp lệ: Thiếu callback_url
    req_no_cb = req_data.copy()
    req_no_cb.pop("callback_url")
    with pytest.raises(ValidationError):
        DispatchRequest.model_validate(req_no_cb)

    ack = DispatchAck(task_id="TASK-001", accepted=True)
    assert ack.accepted is True
    assert ack.reason is None


def test_workflow_run_and_run_task_valid():
    """WorkflowRun và RunTask khởi tạo và kiểm tra timezone aware."""
    run = WorkflowRun(
        run_id="RUN-001",
        project_id="PROJ-Alpha",
        brief="Build full stack app",
    )
    assert run.status == RunStatus.CREATED
    assert run.created_at.tzinfo is not None

    task = RunTask(
        task_id="TASK-001",
        run_id="RUN-001",
        agent_id="tester-01",
        role=AgentRole.TESTER,
        action="create_test_cases",
        instruction="Generate unit tests",
    )
    assert task.dispatch_state == DispatchState.QUEUED
    assert task.attempts == 0
    assert task.version == 1


def test_verdict_model_valid_and_invalid():
    """Verdict nhận PASS/FAIL và danh sách findings chuẩn."""
    valid_verdict = Verdict(
        verdict=VerdictStatus.PASS,
        findings=[
            VerdictFinding(
                severity=FindingSeverity.LOW,
                title="Typo in comment",
                detail="Line 42 has a typo",
            )
        ],
    )
    assert valid_verdict.verdict == VerdictStatus.PASS
    assert len(valid_verdict.findings) == 1

    # Không hợp lệ: verdict sai (không phải PASS hoặc FAIL)
    with pytest.raises(ValidationError):
        Verdict.model_validate({"verdict": "MAYBE", "findings": []})

    # Không hợp lệ: severity sai
    with pytest.raises(ValidationError):
        Verdict.model_validate({
            "verdict": "FAIL",
            "findings": [{"severity": "URGENT", "title": "Crash", "detail": "Null pointer"}],
        })


def test_approval_and_audit_event_valid():
    """Approval và AuditEvent có đầy đủ trường nghiệp vụ."""
    appr = Approval(
        gate="approve_scope",
        status="APPROVED",
        decided_by="admin@ai-team.local",
        reason="Scope looks complete",
    )
    assert appr.gate == "approve_scope"

    audit = AuditEvent(
        actor="system_dispatcher",
        action="DISPATCH_TASK",
        target="dev-01",
        task_id="TASK-001",
        result="SUCCESS",
        details={"machine": "PC4"},
    )
    assert audit.action == "DISPATCH_TASK"
    assert audit.timestamp.tzinfo is not None


# =============================================================================
# 2. KIỂM THỬ ACTION_MAP VÀ CAPABILITY_REGISTRY
# =============================================================================

def test_every_action_in_action_map_has_capability_in_role():
    """Mọi action trong ACTION_MAP phải ánh xạ về một capability thuộc role đó trong CAPABILITY_REGISTRY."""
    for role, actions in ACTION_MAP.items():
        role_caps = set(CAPABILITY_REGISTRY.get(role, []))
        assert role_caps, f"Role {role} không có capability nào trong CAPABILITY_REGISTRY"
        for action_name, cap_name in actions.items():
            assert cap_name in role_caps, (
                f"Action '{action_name}' của role '{role.value}' ánh xạ tới capability '{cap_name}' "
                f"nhưng capability này không nằm trong CAPABILITY_REGISTRY: {role_caps}"
            )


def test_pipeline_actions_present():
    """Tất cả các pipeline action quy định tại Prompt S2 mục 5 đều có mặt trong ACTION_MAP."""
    # DEV
    dev_actions = ACTION_MAP[AgentRole.DEV]
    assert "implement_project" in dev_actions
    assert "implement_feature" in dev_actions
    assert "fix_bugs" in dev_actions

    # TESTER
    tester_actions = ACTION_MAP[AgentRole.TESTER]
    assert "create_test_cases" in tester_actions
    assert "run_test_suite" in tester_actions

    # SECURITY
    assert "security_review" in ACTION_MAP[AgentRole.SECURITY]

    # PO
    po_actions = ACTION_MAP[AgentRole.PO]
    assert "define_backlog" in po_actions
    assert "accept_release" in po_actions

    # BA
    ba_actions = ACTION_MAP[AgentRole.BA]
    assert "write_requirements" in ba_actions
    assert "write_user_stories" in ba_actions

    # SA & DBA
    assert "design_architecture" in ACTION_MAP[AgentRole.SA]
    assert "design_schema" in ACTION_MAP[AgentRole.DBA]

    # DEVOPS
    devops_actions = ACTION_MAP[AgentRole.DEVOPS]
    for act in ["generate_ci_config", "build_image", "deploy", "health_check"]:
        assert act in devops_actions

    # SCRUM_MASTER
    sm_actions = ACTION_MAP[AgentRole.SCRUM_MASTER]
    assert "track_blockers" in sm_actions
    assert "run_sprint_review" in sm_actions
    assert "run_retrospective" in sm_actions


# =============================================================================
# 3. KIỂM THỬ ÁNH XẠ TRẠNG THÁI: DISPATCH_TO_TASK_STATUS
# =============================================================================

def test_dispatch_state_mapping_covers_all_values():
    """Bảng DISPATCH_TO_TASK_STATUS phải bao phủ 100% các giá trị của enum DispatchState."""
    all_dispatch_states = set(DispatchState)
    mapped_states = set(DISPATCH_TO_TASK_STATUS.keys())
    assert all_dispatch_states == mapped_states, (
        f"Thiếu các trạng thái chưa được ánh xạ: {all_dispatch_states - mapped_states}"
    )

    # Kiểm tra tính đúng đắn theo mục 4 SYSTEM_PLAN.md
    assert DISPATCH_TO_TASK_STATUS[DispatchState.QUEUED] == TaskStatus.PENDING
    assert DISPATCH_TO_TASK_STATUS[DispatchState.ASSIGNED] == TaskStatus.PENDING
    assert DISPATCH_TO_TASK_STATUS[DispatchState.RETRY_WAIT] == TaskStatus.PENDING
    assert DISPATCH_TO_TASK_STATUS[DispatchState.RUNNING] == TaskStatus.RUNNING
    assert DISPATCH_TO_TASK_STATUS[DispatchState.COMPLETED] == TaskStatus.COMPLETED
    assert DISPATCH_TO_TASK_STATUS[DispatchState.FAILED] == TaskStatus.FAILED
    assert DISPATCH_TO_TASK_STATUS[DispatchState.BLOCKED] == TaskStatus.BLOCKED
    assert DISPATCH_TO_TASK_STATUS[DispatchState.CANCELLED] == TaskStatus.CANCELLED


# =============================================================================
# 4. KIỂM THỬ VALIDATION ARTIFACT_URI
# =============================================================================

def test_artifact_uri_valid():
    """Các dạng URI hợp lệ: relative, github pull/commit, artifact id."""
    valid_uris = [
        "artifacts/requirements.md",
        "data/report.json",
        "sub/dir/code.zip",
        "github://owner/repo/pull/1",
        "github://vietanh910/AGENT/pull/42",
        "github://owner/repo/commit/a1b2c3d",
        "github://owner/repo/commit/0123456789abcdef0123456789abcdef01234567",
        "artifact://ART-001",
        "artifact://art_schema_v2",
    ]
    for uri in valid_uris:
        assert validate_artifact_uri(uri) == uri


def test_artifact_uri_invalid():
    """Các dạng URI không hợp lệ: path traversal '..', tuyệt đối, sai scheme."""
    invalid_uris = [
        "",
        "   ",
        "../secret.txt",
        "artifacts/../../etc/passwd",
        "/absolute/path/file.txt",
        "C:/Windows/System32/cmd.exe",
        "D:\\data\\file.txt",
        "http://example.com/file.zip",
        "https://example.com/file.zip",
        "file:///home/user/code.zip",
        "ftp://server/archive.tar",
        "github://owner/repo/issues/1",  # Chỉ cho phép pull và commit
        "github://owner/repo/pull/not-a-number",
        "github://owner/repo/pull/../1",  # traversal trong github
        "artifact://ART/../01",  # traversal trong artifact
    ]
    for uri in invalid_uris:
        with pytest.raises(ValueError):
            validate_artifact_uri(uri)


# =============================================================================
# 5. KIỂM THỬ AGENT_REGISTRY_DEFAULT
# =============================================================================

def test_agent_registry_default_complete():
    """AGENT_REGISTRY_DEFAULT chứa đủ 10 agent phân bổ đúng 5 máy theo mục 3."""
    assert len(AGENT_REGISTRY_DEFAULT) == 10

    registry_by_id = {item["agent_id"]: item for item in AGENT_REGISTRY_DEFAULT}

    expected_agents = {
        "pm-01": (AgentRole.PM, "PC1", "LocalWorker"),
        "devops-01": (AgentRole.DEVOPS, "PC1", "LocalWorker"),
        "sa-01": (AgentRole.SA, "PC2", "Worker Host"),
        "dba-01": (AgentRole.DBA, "PC2", "Worker Host"),
        "ba-01": (AgentRole.BA, "PC3", "Worker Host"),
        "po-01": (AgentRole.PO, "PC3", "Worker Host"),
        "dev-01": (AgentRole.DEV, "PC4", "Worker Host"),
        "tester-01": (AgentRole.TESTER, "PC4", "Worker Host"),
        "scrum_master-01": (AgentRole.SCRUM_MASTER, "PC5", "Worker Host"),
        "security-01": (AgentRole.SECURITY, "PC5", "Worker Host"),
    }

    for agent_id, (expected_role, expected_machine, expected_runner) in expected_agents.items():
        assert agent_id in registry_by_id, f"Thiếu agent '{agent_id}' trong AGENT_REGISTRY_DEFAULT"
        item = registry_by_id[agent_id]
        assert item["role"] == expected_role
        assert item["machine"] == expected_machine
        assert item["runner"] == expected_runner


# =============================================================================
# 6. KIỂM THỬ VALIDATION AGENT_ID
# =============================================================================

def test_validate_agent_id_valid_and_invalid():
    """Kiểm tra format <role_viết_thường>-NN và role thuộc enum."""
    valid_ids = [
        "pm-01",
        "devops-01",
        "sa-01",
        "dba-01",
        "ba-01",
        "po-01",
        "dev-01",
        "tester-01",
        "scrum_master-01",
        "security-01",
        "pm-99",
    ]
    for aid in valid_ids:
        assert validate_agent_id(aid) == aid

    invalid_ids = [
        "unknown-01",     # Role không tồn tại
        "developer-01",   # Role DEV chứ không phải DEVELOPER
        "scrum-01",       # Role SCRUM_MASTER chứ không phải SCRUM
        "pm-1",           # Thiếu 2 chữ số
        "pm-001",         # 3 chữ số
        "PM-01",          # Chữ in hoa
        "pm_01",          # Sai dấu gạch nối
        "123-01",         # Số
        "",               # Rỗng
    ]
    for aid in invalid_ids:
        with pytest.raises(ValueError):
            validate_agent_id(aid)


def test_new_error_codes_present():
    """5 ErrorCode mới phải có mặt trong enum ErrorCode."""
    new_codes = [
        ErrorCode.WORKER_OFFLINE,
        ErrorCode.DISPATCH_FAILED,
        ErrorCode.LINEAR_ERROR,
        ErrorCode.GITHUB_ERROR,
        ErrorCode.APPROVAL_REQUIRED,
    ]
    for code in new_codes:
        assert code.value in ErrorCode
