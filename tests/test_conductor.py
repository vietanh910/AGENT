"""test_conductor.py - Bộ kiểm thử tích hợp 13 kịch bản cho PM Conductor.

Tuân thủ Agent Contract v1.1.0 và yêu cầu Prompt 8 mục E:
- Toàn bộ chạy offline, dùng Fake LAN Agents (FastAPI server trên localhost).
- Không gọi LLM thật, không gọi mạng ngoài.
"""

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
pm_dir = ROOT_DIR / "agents" / "pm"
if str(pm_dir) not in sys.path:
    sys.path.insert(0, str(pm_dir))
if str(ROOT_DIR / "tools") not in sys.path:
    sys.path.append(str(ROOT_DIR / "tools"))

import pytest

from agent_contract.enums import AgentRole, AgentStatus, ArtifactType, ErrorCode, TaskStatus
from agent_contract.models import ArtifactRef, TaskRequest, TaskResult
from conductor.dispatcher import load_endpoints
from conductor.gates import GATE_APPROVE_DEPLOY, GATE_APPROVE_SCOPE, should_execute_real_deploy
from conductor.orchestrator import ConductorOrchestrator
from conductor.state import ProjectState, ProjectStatus, StepStatus, load_project_state
from config import PMConfig
from agent import PMAgent
from tests.fake_agents import FakeAgentsServer
from validate_agent import AgentValidationSummary, check_d3_no_inter_agent_access


@pytest.fixture(scope="module")
def fake_server():
    """Khởi động fake agents server cho toàn bộ test suite."""
    server = FakeAgentsServer()
    server.start()
    yield server
    server.stop()


@pytest.fixture(autouse=True)
def reset_server_state(fake_server: FakeAgentsServer):
    """Reset trạng thái của fake server trước mỗi test."""
    fake_server.state.reset()
    yield
    fake_server.state.reset()


@pytest.fixture
def test_pm(fake_server: FakeAgentsServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Tạo instance PM Agent với thư mục dữ liệu tách biệt và endpoints trỏ vào fake server."""
    endpoints = fake_server.get_endpoints()
    monkeypatch.setattr("conductor.dispatcher.load_endpoints", lambda: endpoints)
    monkeypatch.setattr("conductor.orchestrator.load_endpoints", lambda: endpoints)

    cfg = PMConfig(
        base_dir=tmp_path,
        agent_id="pm-01",
        port=8001,
        data_dir=str(tmp_path / "data"),
        artifact_dir=str(tmp_path / "artifacts"),
        workspace_dir=str(tmp_path / "workspace"),
        pm_auto_approve=False,
        pm_allow_real_deploy=False,
        max_fix_loops=3,
        max_tasks_per_project=60,
        project_timeout_minutes=60,
    )
    pm = PMAgent(config=cfg)
    return pm, tmp_path


# =============================================================================
# Kịch bản 1: Luồng chính (Happy Path) -> RELEASE đầy đủ manifest.json
# =============================================================================
def test_1_main_pipeline_happy_path(test_pm):
    pm, tmp_path = test_pm
    proj_id = "PROJ-HAPPY-01"
    brief = "Xây dựng dịch vụ xác thực người dùng và phân quyền vai trò cho hệ thống AI Team."

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.COMPLETED
    assert final_state.block_reason is None

    # Kiểm tra thư mục release
    release_dir = tmp_path / "data" / "projects" / proj_id / "release"
    assert release_dir.exists(), "Thư mục release phải được tạo sau khi hoàn tất pipeline."

    manifest_file = release_dir / "manifest.json"
    assert manifest_file.exists(), "manifest.json phải tồn tại trong release."

    with open(manifest_file, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    assert manifest["project_id"] == proj_id
    assert manifest["status"] == "COMPLETED"
    assert len(manifest["artifacts"]) > 0

    for item in manifest["artifacts"]:
        assert "artifact_id" in item
        assert "sha256" in item
        assert "type" in item

    release_md = release_dir / "RELEASE.md"
    assert release_md.exists(), "RELEASE.md phải được sinh ra."


# =============================================================================
# Kịch bản 2: TESTER FAIL 2 lần rồi PASS -> đúng 2 vòng dev.fix_bugs
# =============================================================================
def test_2_tester_fail_twice_then_pass(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm
    proj_id = "PROJ-FIX-LOOP-02"
    brief = "Dự án thử nghiệm vòng sửa lỗi tự động."

    fake_server.state.tester_failures = 2

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.COMPLETED
    assert final_state.fix_loops_used == 2

    # Đếm số task dev.fix_bugs đã thực thi
    fix_tasks = [
        t for t in fake_server.state.received_tasks
        if t.role == AgentRole.DEV and t.action == "fix_bugs"
    ]
    assert len(fix_tasks) == 2, f"Phải gọi dev.fix_bugs đúng 2 lần, thực tế: {len(fix_tasks)}"


# =============================================================================
# Kịch bản 3: TESTER FAIL liên tục -> dừng ở vòng 3 và BLOCKED
# =============================================================================
def test_3_tester_fail_continuously(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm
    proj_id = "PROJ-FAIL-CONT-03"
    brief = "Dự án kiểm thử giới hạn vòng sửa lỗi tối đa."

    fake_server.state.tester_failures = 10

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.BLOCKED
    assert final_state.fix_loops_used == 3
    assert "MAX_FIX_LOOPS=3" in (final_state.block_reason or "")


# =============================================================================
# Kịch bản 4: Lỗi retryable (mạng chập chờn một lần rồi thành công) -> cùng task_id
# =============================================================================
def test_4_retryable_error_same_task_id(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm
    proj_id = "PROJ-RETRYABLE-04"
    brief = "Dự án thử nghiệm lỗi mạng chập chờn."

    fake_server.state.simulate_retryable_error = 1

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.COMPLETED

    # Kiểm tra bước đầu tiên được gọi 2 lần với cùng một task_id
    po_tasks = [t for t in fake_server.state.received_tasks if t.role == AgentRole.PO and t.action == "define_backlog"]
    assert len(po_tasks) >= 2
    assert po_tasks[0].task_id == po_tasks[1].task_id


# =============================================================================
# Kịch bản 5: Lỗi non-retryable -> BLOCKED ngay, bước sau không chạy
# =============================================================================
def test_5_non_retryable_error_blocks(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm
    proj_id = "PROJ-NON-RETRY-05"
    brief = "Dự án gặp lỗi nghiêm trọng không thể thử lại."

    fake_server.state.simulate_non_retryable_error = True

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.BLOCKED
    assert "Lỗi hệ thống nghiêm trọng" in (final_state.block_reason or "")

    # Chỉ có 1 task được gửi trước khi dừng
    assert len(fake_server.state.received_tasks) == 1


# =============================================================================
# Kịch bản 6: Agent đích không bật (mất kết nối) -> thử lại rồi BLOCKED
# =============================================================================
def test_6_agent_unreachable(test_pm, monkeypatch: pytest.MonkeyPatch):
    pm, _ = test_pm
    proj_id = "PROJ-DEAD-AGENT-06"
    brief = "Dự án gọi agent bị tắt máy."

    # Ánh xạ endpoint về cổng không có server lắng nghe
    dead_endpoints = {k: "http://127.0.0.1:59999" for k in [
        "po-01", "ba-01", "sa-01", "dba-01", "dev-01", "tester-01", "security-01", "devops-01", "scrum_master-01"
    ]}
    monkeypatch.setattr("conductor.dispatcher.load_endpoints", lambda: dead_endpoints)
    monkeypatch.setattr("conductor.orchestrator.load_endpoints", lambda: dead_endpoints)

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.BLOCKED
    assert "Không thể kết nối" in (final_state.block_reason or "")


# =============================================================================
# Kịch bản 7: Dừng tại GATE approve_scope -> tiếp tục khi approve; auto-approve đi thẳng
# =============================================================================
def test_7_gate_approve_scope_and_auto_approve(test_pm):
    pm, _ = test_pm

    # 1. Chạy không có auto-approve -> phải dừng ở approve_scope
    proj_manual = "PROJ-GATE-MANUAL-07"
    state_m = pm.conductor.init_project(proj_manual, "Brief dự án duyệt thủ công", auto_approve=False)
    state_m = pm.conductor.run_pipeline(state_m)

    assert state_m.status == ProjectStatus.WAITING_APPROVAL
    assert state_m.waiting_gate == GATE_APPROVE_SCOPE

    # Phê duyệt thủ công cổng approve_scope
    state_m = pm.conductor.approve_gate(proj_manual, GATE_APPROVE_SCOPE)
    # Sau đó sẽ dừng ở cổng thứ 2: approve_deploy
    assert state_m.status == ProjectStatus.WAITING_APPROVAL
    assert state_m.waiting_gate == GATE_APPROVE_DEPLOY

    # 2. Chạy với auto-approve -> đi thẳng qua cả 2 cổng tới COMPLETED
    proj_auto = "PROJ-GATE-AUTO-07"
    state_a = pm.conductor.init_project(proj_auto, "Brief dự án auto-approve", auto_approve=True)
    state_a = pm.conductor.run_pipeline(state_a)
    assert state_a.status == ProjectStatus.COMPLETED


# =============================================================================
# Kịch bản 8: PM ngắt tiến trình, gọi resume: chạy tiếp từ bước dở dang, KHÔNG chạy lại
# =============================================================================
def test_8_resume_after_process_interruption(test_pm, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_server: FakeAgentsServer):
    pm, _ = test_pm
    proj_id = "PROJ-RESUME-08"

    # Chạy tới khi dừng ở cổng approve_scope
    state = pm.conductor.init_project(proj_id, "Brief kiểm tra resume sau sự cố", auto_approve=False)
    state = pm.conductor.run_pipeline(state)
    assert state.status == ProjectStatus.WAITING_APPROVAL

    # Ghi nhận số task đã hoàn thành trước khi ngắt tiến trình
    tasks_before = len(fake_server.state.received_tasks)
    assert tasks_before >= 3  # PO define backlog, BA requirements, BA user stories

    # Giả lập khởi động lại tiến trình PM (tạo mới PMAgent từ cấu hình đĩa)
    endpoints = fake_server.get_endpoints()
    monkeypatch.setattr("conductor.dispatcher.load_endpoints", lambda: endpoints)
    monkeypatch.setattr("conductor.orchestrator.load_endpoints", lambda: endpoints)

    new_cfg = PMConfig(
        base_dir=tmp_path,
        agent_id="pm-01",
        port=8001,
        data_dir=str(tmp_path / "data"),
        artifact_dir=str(tmp_path / "artifacts"),
        workspace_dir=str(tmp_path / "workspace"),
        pm_auto_approve=False,  # Cho phép tự động sau khi khôi phục
    )
    new_pm = PMAgent(config=new_cfg)

    # Duyệt cổng approve_scope và tiếp tục chạy
    resumed_state = new_pm.conductor.approve_gate(proj_id, GATE_APPROVE_SCOPE)
    assert resumed_state.status == ProjectStatus.WAITING_APPROVAL
    assert resumed_state.waiting_gate == GATE_APPROVE_DEPLOY

    # Duyệt tiếp cổng approve_deploy để hoàn tất dự án
    completed_state = new_pm.conductor.approve_gate(proj_id, GATE_APPROVE_DEPLOY)
    assert completed_state.status == ProjectStatus.COMPLETED

    # Kiểm tra các bước trước cổng (po_define_backlog, ba_write_requirements, ba_write_user_stories)
    # không bị chạy lại lần 2
    assert completed_state.steps["po_define_backlog"].attempts == 1
    assert completed_state.steps["ba_write_requirements"].attempts == 1
    assert completed_state.steps["ba_write_user_stories"].attempts == 1


# =============================================================================
# Kịch bản 9: Checksum artifact sai lệch -> PM từ chối, bước đó thất bại
# =============================================================================
def test_9_artifact_checksum_mismatch(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm
    proj_id = "PROJ-BAD-HASH-09"
    brief = "Kiểm tra phát hiện sai lệch checksum artifact."

    fake_server.state.corrupt_checksum = True

    state = pm.conductor.init_project(proj_id, brief, auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.BLOCKED
    assert "checksum" in (final_state.block_reason or "").lower()


# =============================================================================
# Kịch bản 10: Trả lời MISSING_CONTEXT: PM dùng LLM trả lời 1 lần từ brief; lần 2 BLOCKED
# =============================================================================
def test_10_missing_context_answered_by_pm(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm

    # 1. Thiếu context 1 lần: PM trả lời từ brief -> thành công
    proj_once = "PROJ-MISSING-ONCE-10"
    fake_server.state.simulate_missing_context = 1
    state_1 = pm.conductor.init_project(proj_once, "Brief dự án có phạm vi phân quyền rõ ràng", auto_approve=True)
    state_1 = pm.conductor.run_pipeline(state_1)
    assert state_1.status == ProjectStatus.COMPLETED

    # 2. Thiếu context 2 lần liên tiếp: PM dừng và lưu danh sách câu hỏi
    fake_server.state.reset()
    proj_twice = "PROJ-MISSING-TWICE-10"
    fake_server.state.simulate_missing_context = 2
    state_2 = pm.conductor.init_project(proj_twice, "Brief dự án thiếu thông tin", auto_approve=True)
    state_2 = pm.conductor.run_pipeline(state_2)
    assert state_2.status == ProjectStatus.BLOCKED
    assert len(state_2.missing_questions) > 0


# =============================================================================
# Kịch bản 11: Vượt quá MAX_TASKS_PER_PROJECT -> dừng và BLOCKED
# =============================================================================
def test_11_max_tasks_per_project_limit(test_pm):
    pm, _ = test_pm
    pm.config.max_tasks_per_project = 2

    proj_id = "PROJ-MAX-TASKS-11"
    state = pm.conductor.init_project(proj_id, "Brief kiểm tra giới hạn số tác vụ", auto_approve=True)
    final_state = pm.conductor.run_pipeline(state)

    assert final_state.status == ProjectStatus.BLOCKED
    assert "MAX_TASKS_PER_PROJECT" in (final_state.block_reason or "")


# =============================================================================
# Kịch bản 12: DevOps deploy: execute=false trừ khi PM_ALLOW_REAL_DEPLOY=true VÀ approve_deploy đã duyệt
# =============================================================================
def test_12_devops_deploy_execution_flag(test_pm, fake_server: FakeAgentsServer):
    pm, _ = test_pm

    # 1. Mặc định PM_ALLOW_REAL_DEPLOY = False -> context.execute phải là False
    proj_dry = "PROJ-DEPLOY-DRY-12"
    state_dry = pm.conductor.init_project(proj_dry, "Brief deploy dry-run", auto_approve=True)
    pm.conductor.run_pipeline(state_dry)

    assert len(fake_server.state.deploy_received_contexts) > 0
    for ctx in fake_server.state.deploy_received_contexts:
        assert ctx.get("execute") is False, "Mặc định không bao giờ gửi execute=True"

    # 2. Bật PM_ALLOW_REAL_DEPLOY = True nhưng chưa duyệt cổng -> should_execute_real_deploy trả False
    assert not should_execute_real_deploy(state_dry, allow_real_deploy_config=False)
    assert not should_execute_real_deploy(ProjectState(project_id="test", brief="test"), allow_real_deploy_config=True)


# =============================================================================
# Kịch bản 13: Isolation: Agent khác (ví dụ devops) import conductor -> bị validator chặn
# =============================================================================
def test_13_isolation_non_pm_conductor_forbidden(tmp_path: Path):
    # PM được phép
    pm_dir = tmp_path / "pm"
    pm_dir.mkdir(parents=True, exist_ok=True)
    (pm_dir / "agent.py").write_text("from conductor.orchestrator import ConductorOrchestrator\n", encoding="utf-8")
    summary_pm = AgentValidationSummary(agent_dir=str(pm_dir))
    check_d3_no_inter_agent_access(pm_dir, summary_pm)
    assert summary_pm.passed is True

    # Agent khác (DevOps, Dev, Tester...) bị cấm
    for non_pm_role in ["DEVOPS", "DEV", "TESTER", "BA", "SA", "DBA", "PO", "SECURITY", "SCRUM_MASTER"]:
        role_dir = tmp_path / non_pm_role.lower()
        role_dir.mkdir(parents=True, exist_ok=True)
        (role_dir / "agent.py").write_text("from conductor.orchestrator import ConductorOrchestrator\n", encoding="utf-8")
        summary_role = AgentValidationSummary(agent_dir=str(role_dir))
        check_d3_no_inter_agent_access(role_dir, summary_role)
        assert summary_role.passed is False
        assert any("conductor" in c.message or "D3:" in c.message for c in summary_role.checks)
