"""control/tests/test_devops_workflow.py - Kiểm thử toàn diện DevOps Agent và Quy trình Triển khai Staging (Prompt S10).

Các kịch bản kiểm thử:
1. Thiếu điều kiện thì không deploy:
   - Thiếu TESTER PASS -> không giao task deploy.
   - Thiếu SECURITY PASS -> không giao task deploy.
2. CI đỏ trên GitHub PR -> không merge PR và không giao deploy.
3. Lỗi build Docker -> trả về FAILED với ErrorCode.TOOL_ERROR.
4. Health check FAIL -> không báo thành công, trả về FAILED TOOL_ERROR.
5. Bộ cấp cổng (PortAllocator) -> tránh đụng nhau giữa các run song song.
6. Dry-run mode -> không thực thi bất kỳ lệnh hệ thống thật nào.
7. Triển khai Production bị chặn khi thiếu cờ ALLOW_PROD_DEPLOY hoặc thiếu duyệt gate approve_prod.
8. Cập nhật Linear: issue deploy được thêm comment chứa URL, version và status.
9. Kiểm thử Docker THẬT (@pytest.mark.docker): Đóng gói image mẫu, triển khai Staging stack, kiểm tra health check HTTP 200 OK, và dọn dẹp an toàn.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import fakeredis
import httpx
import pytest
from sqlalchemy import select

from agent_contract.enums import AgentRole, AgentStatus, ArtifactType, ErrorCode, Priority, TaskStatus
from agent_contract.models import Constraints, TaskRequest, TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus
from agents.devops.agent import DevOpsAgent
from agents.devops.config import DevOpsConfig
from agents.devops.schemas import DeploymentReport, HealthCheckReport
from control.app.config import settings
from control.app.database.models import (
    AgentModel,
    ApprovalModel,
    LinearLinkModel,
    TaskModel,
    WorkerModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.session import get_session_factory
from control.app.integrations.github.fake import FakeGitHubClient
from control.app.integrations.linear.client import FakeWorkTracker
from control.app.orchestrator.scheduler import SchedulerService
from control.app.services.deploy_guard import DeployGuardService
from control.app.services.dispatcher import DispatcherService
from control.app.services.staging_manager import PortAllocator, StagingManager
from tools.terminal import CommandExecutionError, TerminalExecutor


@pytest.fixture
def session_factory():
    return get_session_factory()


def make_devops_agent(tmp_path: Path, dry_run: bool = True) -> DevOpsAgent:
    """Tạo DevOpsAgent chuẩn với cấu hình an toàn trong tmp_path."""
    ws = tmp_path / "workspace"
    ws.mkdir(parents=True, exist_ok=True)
    art = tmp_path / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    cfg = DevOpsConfig(
        base_dir=tmp_path,
        port=8002,
        agent_id="devops-01",
        agent_role=AgentRole.DEVOPS,
        workspace_dir=str(ws),
        artifact_dir=str(art),
        dry_run=dry_run,
    )
    return DevOpsAgent(
        config=cfg,
        workspace_dir=cfg.workspace_dir,
        artifact_dir=cfg.artifact_dir,
    )


@pytest.fixture
def clean_db(session_factory):
    """Đảm bảo các agent chuẩn devops-01, tester-01, security-01 tồn tại trong MySQL."""
    with session_factory() as session:
        # Đảm bảo worker PC1 online
        w1 = session.get(WorkerModel, "worker-pc1")
        if not w1:
            w1 = WorkerModel(
                worker_id="worker-pc1",
                machine_id="PC1",
                hostname="pc1-control",
                base_url="http://127.0.0.1:8000",
                status="ONLINE",
                last_heartbeat=utc_now(),
            )
            session.add(w1)
        else:
            w1.status = "ONLINE"
            w1.last_heartbeat = utc_now()

        # Đảm bảo agent devops-01
        devops = session.get(AgentModel, "devops-01")
        if not devops:
            devops = AgentModel(
                agent_id="devops-01",
                role="DEVOPS",
                machine="PC1",
                capabilities=["docker", "deployment", "ci_cd", "health_check", "monitoring", "git"],
                status="IDLE",
            )
            session.add(devops)

        session.commit()
    yield
    # Cleanup runs if needed


# -----------------------------------------------------------------------------
# 1. Thiếu điều kiện thì không deploy
# -----------------------------------------------------------------------------
def test_missing_conditions_block_deploy(session_factory, clean_db):
    """Thiếu TESTER PASS hoặc SECURITY PASS thì không được phép deploy."""
    run_id = f"test_dep_precond_{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        run = WorkflowRunModel(
            run_id=run_id,
            project_id="PROJ-TEST",
            brief="Test deploy conditions",
            status=RunStatus.RUNNING.value,
        )
        session.add(run)

        # 1a. Chưa có task TESTER nào
        deploy_task = TaskModel(
            task_id=f"{run_id}_deploy",
            run_id=run_id,
            agent_id="devops-01",
            role="DEVOPS",
            action="deploy",
            instruction="Triển khai staging",
            dispatch_state=DispatchState.QUEUED.value,
            timeout_seconds=300,
        )
        session.add(deploy_task)
        session.commit()

        # Kiểm tra DeployGuard
        ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, run_id)
        assert ok is False
        assert "TESTER" in reason

        # Scheduler không trả về deploy_task là READY
        scheduler = SchedulerService(max_parallel_tasks=2)
        ready = scheduler.get_ready_tasks_for_run(session, run_id)
        assert deploy_task.task_id not in [t.task_id for t in ready]

        # 1b. Có task TESTER nhưng verdict FAIL
        tester_task = TaskModel(
            task_id=f"{run_id}_tester",
            run_id=run_id,
            agent_id="tester-01",
            role="TESTER",
            action="run_test_suite",
            instruction="Run tests",
            dispatch_state=DispatchState.COMPLETED.value,
            result={"verdict": "FAIL", "failed_count": 2},
            timeout_seconds=300,
        )
        session.add(tester_task)
        session.commit()

        ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, run_id)
        assert ok is False
        assert "TESTER" in reason

        # 1c. TESTER PASS nhưng chưa có SECURITY task
        tester_task.result = {"verdict": "PASS"}
        session.commit()

        ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, run_id)
        assert ok is False
        assert "SECURITY" in reason

        # 1d. SECURITY verdict FAIL hoặc có lỗ hổng CRITICAL
        sec_task = TaskModel(
            task_id=f"{run_id}_security",
            run_id=run_id,
            agent_id="security-01",
            role="SECURITY",
            action="security_review",
            instruction="Security review",
            dispatch_state=DispatchState.COMPLETED.value,
            result={"verdict": "FAIL", "findings": [{"severity": "CRITICAL", "desc": "SQL injection"}]},
            timeout_seconds=300,
        )
        session.add(sec_task)
        session.commit()

        ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, run_id)
        assert ok is False
        assert "SECURITY" in reason

        # 1e. Cả TESTER PASS và SECURITY PASS -> Điều kiện 1 & 2 đạt
        sec_task.result = {"verdict": "PASS", "findings": []}
        session.commit()

        ok, reason, _ = DeployGuardService.verify_and_merge_pr(session, run_id)
        assert ok is True


# -----------------------------------------------------------------------------
# 2. CI đỏ trên GitHub PR thì không merge
# -----------------------------------------------------------------------------
def test_red_ci_blocks_pr_merge(session_factory, clean_db):
    """Khi CI của PR trên GitHub bị đỏ (failure), không merge PR và từ chối deploy."""
    run_id = f"test_ci_red_{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        run = WorkflowRunModel(
            run_id=run_id,
            project_id="PROJ-TEST",
            brief="Test Red CI",
            status=RunStatus.RUNNING.value,
            run_metadata={"pr_number": 42, "head_sha": "abc123red"},
        )
        session.add(run)

        # TESTER và SECURITY đều PASS
        session.add(
            TaskModel(
                task_id=f"{run_id}_tester",
                run_id=run_id,
                agent_id="tester-01",
                role="TESTER",
                action="run_test_suite",
                instruction="Chạy kiểm thử tự động",
                dispatch_state=DispatchState.COMPLETED.value,
                result={"verdict": "PASS"},
            )
        )
        session.add(
            TaskModel(
                task_id=f"{run_id}_security",
                run_id=run_id,
                agent_id="security-01",
                role="SECURITY",
                action="security_review",
                instruction="Đánh giá an toàn bảo mật",
                dispatch_state=DispatchState.COMPLETED.value,
                result={"verdict": "PASS", "findings": []},
            )
        )
        session.commit()

        # Setup Fake GitHub Client với Check Run thất bại
        fake_gh = FakeGitHubClient()
        fake_gh.pull_requests["vietanh910/AGENT"] = [
            {
                "number": 42,
                "title": "PR for Run",
                "head": {"ref": "abc123red"},
                "state": "open",
                "merged": False,
            }
        ]
        fake_gh.check_runs["abc123red"] = [
            {"id": 101, "name": "CI / unit-tests", "status": "completed", "conclusion": "failure"}
        ]

        ok, reason, data = DeployGuardService.verify_and_merge_pr(
            session=session,
            run_id=run_id,
            github_client=fake_gh,
            owner="vietanh910",
            repo="AGENT",
            timeout_seconds=0.5,
        )
        assert ok is False
        assert "CI của Pull Request trên GitHub bị đỏ" in reason
        assert fake_gh.pull_requests["vietanh910/AGENT"][0]["merged"] is False


# -----------------------------------------------------------------------------
# 3. Lỗi build Docker thì trả về FAILED TOOL_ERROR
# -----------------------------------------------------------------------------
def test_docker_build_failure_returns_tool_error(tmp_path):
    """Khi quá trình build Docker thất bại (exit code != 0), agent trả về FAILED với TOOL_ERROR."""
    agent = make_devops_agent(tmp_path, dry_run=False)

    # Giả lập lệnh docker build trả về exit code 1
    mock_err = CommandExecutionError(
        message="Lệnh docker build thất bại với exit code 1",
        exit_code=1,
        stdout="",
        stderr="Step 3/5: RUN invalid_command - not found",
        retryable=False,
    )

    with patch.object(agent.terminal, "run", side_effect=mock_err):
        req = TaskRequest(
            task_id="TASK-BUILD-FAIL",
            project_id="PROJ-01",
            agent_id="devops-01",
            role=AgentRole.DEVOPS,
            action="build_image",
            instruction="Build docker image",
            context={"tag": "app:v1", "execute": True},
        )
        res = agent.handle(req)

        assert res.status == TaskStatus.FAILED
        assert res.error is not None
        assert res.error.code == ErrorCode.TOOL_ERROR
        assert res.error.details.get("exit_code") == 1


# -----------------------------------------------------------------------------
# 4. Health check FAIL thì không báo thành công
# -----------------------------------------------------------------------------
def test_health_check_fail_does_not_report_success(tmp_path):
    """Khi health check gọi thất bại qua các lần thử lại, trả về FAILED TOOL_ERROR."""
    agent = make_devops_agent(tmp_path, dry_run=False)

    req = TaskRequest(
        task_id="TASK-HEALTH-FAIL",
        project_id="PROJ-01",
        agent_id="devops-01",
        role=AgentRole.DEVOPS,
        action="health_check",
        instruction="Kiểm tra health check",
        context={
            "endpoint": "http://127.0.0.1:9999/health",  # Cổng không tồn tại
            "timeout_seconds": 0.2,
            "max_retries": 2,
            "retry_delay": 0.1,
            "execute": True,
        },
    )
    res = agent.handle(req)

    assert res.status == TaskStatus.FAILED
    assert res.error is not None
    assert res.error.code == ErrorCode.TOOL_ERROR
    assert "Health check thất bại" in res.summary


# -----------------------------------------------------------------------------
# 5. Bộ cấp cổng (PortAllocator) tránh đụng nhau giữa các run
# -----------------------------------------------------------------------------
def test_port_allocator_concurrent_runs():
    """Bộ cấp cổng cấp phát các cổng riêng biệt trong dải [8100, 8900] cho từng run."""
    allocator = PortAllocator(port_start=8100, port_end=8110)
    allocator.reset()

    port_run1 = allocator.allocate_port("run_1")
    port_run2 = allocator.allocate_port("run_2")
    port_run3 = allocator.allocate_port("run_3")

    assert 8100 <= port_run1 <= 8110
    assert 8100 <= port_run2 <= 8110
    assert 8100 <= port_run3 <= 8110

    # Không đụng nhau
    assert len({port_run1, port_run2, port_run3}) == 3

    # Cấp lại cho run_1 vẫn trả về đúng cổng cũ
    assert allocator.allocate_port("run_1") == port_run1

    # Thu hồi cổng run_2
    rel = allocator.release_port("run_2")
    assert rel == port_run2

    # Thu hồi xong có thể cấp phát lại cổng đó cho run mới
    port_run4 = allocator.allocate_port("run_4")
    assert port_run4 == port_run2
    allocator.reset()


# -----------------------------------------------------------------------------
# 6. Dry-run mode không thực thi lệnh hệ thống thật
# -----------------------------------------------------------------------------
def test_dry_run_executes_no_subprocess(tmp_path):
    """DevOpsAgent ở chế độ dry-run tuyệt đối không gọi thực thi lệnh hệ thống thật."""
    agent = make_devops_agent(tmp_path, dry_run=True)

    with patch("subprocess.run") as mock_subproc:
        req = TaskRequest(
            task_id="TASK-DRY-RUN",
            project_id="PROJ-01",
            agent_id="devops-01",
            role=AgentRole.DEVOPS,
            action="deploy",
            instruction="Triển khai staging",
            context={
                "environment": "staging",
                "service_name": "mock-app",
                "rollback_plan": ["docker compose down"],
                "execute": False,
            },
        )
        res = agent.handle(req)

        mock_subproc.assert_not_called()
        assert res.status == TaskStatus.COMPLETED
        report_data = res.outputs[0]
        assert report_data.get("status") == "DRY_RUN"


# -----------------------------------------------------------------------------
# 7. Triển khai Production bị chặn khi thiếu cờ hoặc thiếu duyệt
# -----------------------------------------------------------------------------
def test_production_deploy_blocked_without_flag_or_approval(tmp_path):
    """Triển khai lên Production bị chặn với PERMISSION_DENIED (thiếu cờ) hoặc APPROVAL_REQUIRED (thiếu duyệt)."""
    agent = make_devops_agent(tmp_path, dry_run=True)

    # 7a. Thiếu cờ ALLOW_PROD_DEPLOY=false
    req_no_flag = TaskRequest(
        task_id="TASK-PROD-NO-FLAG",
        project_id="PROJ-01",
        agent_id="devops-01",
        role=AgentRole.DEVOPS,
        action="deploy",
        instruction="Triển khai production",
        context={
            "environment": "production",
            "service_name": "core-api",
            "rollback_plan": ["docker compose down"],
            "allow_prod_deploy": False,
            "gate_approved": True,
        },
    )
    res_no_flag = agent.handle(req_no_flag)
    assert res_no_flag.status == TaskStatus.BLOCKED
    assert res_no_flag.error.code == ErrorCode.PERMISSION_DENIED
    assert res_no_flag.error.retryable is False

    # 7b. Có cờ ALLOW_PROD_DEPLOY=true nhưng CHƯA duyệt gate approve_prod
    req_no_approval = TaskRequest(
        task_id="TASK-PROD-NO-APPROVAL",
        project_id="PROJ-01",
        agent_id="devops-01",
        role=AgentRole.DEVOPS,
        action="deploy",
        instruction="Triển khai production",
        context={
            "environment": "production",
            "service_name": "core-api",
            "rollback_plan": ["docker compose down"],
            "allow_prod_deploy": True,
            "gate_approved": False,
        },
    )
    res_no_approval = agent.handle(req_no_approval)
    assert res_no_approval.status == TaskStatus.BLOCKED
    assert res_no_approval.error.code == ErrorCode.APPROVAL_REQUIRED
    assert res_no_approval.error.retryable is False


# -----------------------------------------------------------------------------
# 8. Cập nhật Linear khi deploy thành công
# -----------------------------------------------------------------------------
def test_post_deployment_updates_linear(session_factory):
    """Khi deploy thành công, comment chứa URL, version và status được đăng lên Linear issue."""
    run_id = f"test_linear_dep_{uuid.uuid4().hex[:6]}"
    with session_factory() as session:
        # Tạo liên kết LinearLinkModel
        link = LinearLinkModel(
            entity_type="run",
            local_id=run_id,
            linear_id="ISSUE-LIN-999",
            content_hash="abc",
        )
        session.add(link)
        session.commit()

        fake_tracker = FakeWorkTracker()
        fake_tracker.issues["ISSUE-LIN-999"] = {
            "id": "ISSUE-LIN-999",
            "title": "Deploy Issue",
            "comments": [],
            "links": [],
        }

        ok = DeployGuardService.post_deployment_to_linear(
            session=session,
            run_id=run_id,
            deployed_url="http://127.0.0.1:8150",
            version="web-v1.0.0",
            status="HEALTHY",
            tracker=fake_tracker,
        )

        assert ok is True
        matching_comments = [
            c for c in fake_tracker.comments.values()
            if c.get("issue_id") == "ISSUE-LIN-999"
        ]
        assert len(matching_comments) >= 1
        assert "http://127.0.0.1:8150" in matching_comments[0]["body"]
        assert "web-v1.0.0" in matching_comments[0]["body"]

        matching_attachments = [
            a for a in fake_tracker.attachments.values()
            if a.get("issue_id") == "ISSUE-LIN-999"
        ]
        assert len(matching_attachments) >= 1
        assert matching_attachments[0]["url"] == "http://127.0.0.1:8150"


# -----------------------------------------------------------------------------
# 9. Kiểm thử Docker THẬT (@pytest.mark.docker)
# -----------------------------------------------------------------------------
@pytest.mark.docker
def test_real_docker_staging_deployment(tmp_path):
    """Triển khai Staging bằng Docker THẬT trên PC1 host:
    - Build image mẫu từ Dockerfile thực tế
    - StagingManager chuẩn bị stack cách ly và cấp phát cổng
    - Chạy docker compose up -d
    - Kiểm tra Health check HTTP 200 OK
    - Dọn dẹp stack an toàn (teardown)
    """
    # 1. Chuẩn bị Dockerfile ứng dụng mẫu nhẹ (sử dụng nginx:alpine)
    app_dir = tmp_path / "sample_app"
    app_dir.mkdir(parents=True, exist_ok=True)
    df_path = app_dir / "Dockerfile"
    df_path.write_text(
        "FROM nginx:alpine\n"
        "RUN echo '{\"status\":\"ok\",\"service\":\"sample-staging\"}' > /usr/share/nginx/html/health\n",
        encoding="utf-8",
    )

    image_tag = "sample-staging-real:latest"
    terminal = TerminalExecutor(
        workspace_dir=str(app_dir),
        allowed_commands=["docker"],
        dry_run=False,
    )

    # 2. Build Docker Image thật
    build_res = terminal.run(
        ["docker", "build", "-t", image_tag, str(app_dir)],
        execute=True,
        timeout=120,
    )
    assert build_res.exit_code == 0

    # 3. Quản lý Staging với StagingManager
    staging_dir = tmp_path / "staging_deploy"
    staging_mgr = StagingManager(base_staging_dir=staging_dir)
    run_id = f"real_run_{uuid.uuid4().hex[:6]}"

    # Chuẩn bị stack tại deploy/staging/<run_id>/
    st_dir, allocated_port = staging_mgr.prepare_staging_stack(
        run_id=run_id,
        image_tag=image_tag,
        container_port=80,
        env_vars={"APP_NAME": "sample-staging"},
        terminal=terminal,
        execute=True,
    )
    assert st_dir.exists()
    assert (st_dir / "docker-compose.yml").exists()
    assert (st_dir / ".env").exists()

    # 4. Triển khai stack thật qua docker compose
    deploy_res = staging_mgr.deploy_stack(run_id=run_id, terminal=terminal, execute=True)
    assert deploy_res.exit_code == 0

    try:
        # 5. Chờ và kiểm định Health Check bằng httpx
        health_url = f"http://127.0.0.1:{allocated_port}/health"
        time.sleep(1.5)

        # Sử dụng tool health_check của DevOps
        from agents.devops.tools.deployment import health_check
        hc_report = health_check(
            endpoint=health_url,
            expected_status=200,
            timeout=5.0,
            max_retries=5,
            retry_delay=1.0,
            dry_run=False,
        )

        assert hc_report.status == "HEALTHY"
        assert hc_report.status_code == 200
        assert "sample-staging" in hc_report.response_body_sample

    finally:
        # 6. Dọn dẹp stack an toàn (Teardown)
        down_res = staging_mgr.teardown_stack(run_id=run_id, terminal=terminal, execute=True)
        assert down_res.exit_code == 0
        assert not st_dir.exists()
