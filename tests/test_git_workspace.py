"""Bộ kiểm thử cho Git Workspace và GitHub Integration (tests/test_git_workspace.py).

Kiểm tra:
1. GitWorkspace tương tác với bare repo cục bộ qua URL file:// (clone, branch, commit, push, checkout).
2. Chặn tuyệt đối push trực tiếp lên branch main/master.
3. Chặn tuyệt đối force push (--force, -f).
4. GitHub Integration: tạo PR, kiểm tra chữ ký webhook, kiểm tra policy guard khi merge PR.
5. Merge PR bị từ chối khi policy_ok=False, thành công khi policy_ok=True (ghi nhận audit log).
6. Tương tác phối hợp giữa DEV (tạo branch, mở PR) và TESTER (checkout PR, chạy test).
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Generator

# Đảm bảo root dự án nằm trong sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

agent_template_dir = repo_root / "agent-template"
import tools
agent_tools_dir = str(agent_template_dir / "tools")
if agent_tools_dir not in tools.__path__:
    tools.__path__.insert(0, agent_tools_dir)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from agent_contract.enums import AgentRole, ArtifactType, TaskStatus
from agent_contract.models import TaskRequest
from tools.dev_git_actions import (
    execute_dev_coding_action,
    execute_tester_run_action,
)
from tools.git_workspace import GitWorkspace, sanitize_branch_name
from tools.terminal import SecurityViolationError, TerminalExecutor
from control.app.database.models import AuditLogModel, Base
from control.app.database.repositories import AuditRepository
from control.app.integrations.github.client import GitHubPolicyError
from control.app.integrations.github.fake import FakeGitHubClient
from control.app.integrations.github.pull_requests import merge_pull_request
from control.app.integrations.github.webhook import verify_github_webhook_signature


@pytest.fixture
def test_git_env() -> Generator[dict[str, Path], None, None]:
    """Tạo môi trường git gồm bare remote repo và các thư mục workspace."""
    base_tmp = Path(tempfile.mkdtemp(prefix="test_git_env_"))
    bare_remote = base_tmp / "remote.git"
    dev_ws = base_tmp / "dev_ws"
    tester_ws = base_tmp / "tester_ws"

    # 1. Khởi tạo bare repository
    bare_remote.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare"], cwd=str(bare_remote), check=True, capture_output=True)

    # 2. Tạo một commit ban đầu trên main để có HEAD hợp lệ
    seed_dir = base_tmp / "seed"
    seed_dir.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(seed_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Seed User"], cwd=str(seed_dir), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "seed@ai-team.local"], cwd=str(seed_dir), check=True, capture_output=True)
    (seed_dir / "README.md").write_text("# Initial Repository\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(seed_dir), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit on main"], cwd=str(seed_dir), check=True, capture_output=True)
    subprocess.run(["git", "branch", "-M", "main"], cwd=str(seed_dir), check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare_remote.as_uri())], cwd=str(seed_dir), check=True, capture_output=True)
    subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(seed_dir), check=True, capture_output=True)

    shutil.rmtree(seed_dir, ignore_errors=True)

    yield {
        "base": base_tmp,
        "remote_url": str(bare_remote.as_uri()),
        "bare_remote": bare_remote,
        "dev_ws": dev_ws,
        "tester_ws": tester_ws,
    }

    shutil.rmtree(base_tmp, ignore_errors=True)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSession()
    try:
        yield session
    finally:
        session.close()


class TestGitWorkspaceSafety:
    """Kiểm tra các ràng buộc an ninh nghiêm ngặt trên GitWorkspace."""

    def test_forbid_push_to_main_or_master(self, test_git_env: dict[str, Path]):
        git_ws = GitWorkspace(workspace_dir=test_git_env["dev_ws"])
        git_ws.clone_or_init(remote_url=test_git_env["remote_url"])

        # Cố tình push lên main -> Ném SecurityViolationError
        with pytest.raises(SecurityViolationError) as exc_info:
            git_ws.push(remote="origin", branch="main")
        assert "TUYỆT ĐỐI BỊ CẤM push trực tiếp lên branch 'main'" in str(exc_info.value)

        with pytest.raises(SecurityViolationError) as exc_info:
            git_ws.push(remote="origin", branch="master")
        assert "TUYỆT ĐỐI BỊ CẤM push trực tiếp lên branch 'master'" in str(exc_info.value)

    def test_forbid_force_push_in_terminal_executor(self, test_git_env: dict[str, Path]):
        terminal = TerminalExecutor(workspace_dir=test_git_env["dev_ws"])

        # Cố tình dùng --force
        with pytest.raises(SecurityViolationError) as exc_info:
            terminal.run(["git", "push", "--force", "origin", "my-feature"])
        assert "mẫu lệnh nguy hiểm" in str(exc_info.value).lower()

        # Cố tình dùng -f
        with pytest.raises(SecurityViolationError) as exc_info:
            terminal.run(["git", "push", "-f", "origin", "my-feature"])
        assert "mẫu lệnh nguy hiểm" in str(exc_info.value).lower()


class TestGitHubAdapterAndPolicyGuard:
    """Kiểm tra GitHub Adapter, Webhook Signature và cơ chế Merge có Policy Guard."""

    def test_verify_webhook_signature(self):
        secret = "my_webhook_secret_key"
        payload = b'{"action": "opened", "issue": {"number": 1}}'

        import hashlib
        import hmac
        expected_sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        # Chữ ký chuẩn -> PASS
        assert verify_github_webhook_signature(payload, expected_sig, secret) is True

        # Chữ ký sai lệch -> FAIL
        assert verify_github_webhook_signature(payload, "sha256=wrongsignature", secret) is False

        # Thiếu secret hoặc header -> FAIL
        assert verify_github_webhook_signature(payload, None, secret) is False
        assert verify_github_webhook_signature(payload, expected_sig, "") is False

    def test_merge_pr_requires_policy_ok(self, db_session: Session):
        fake_client = FakeGitHubClient()
        fake_client.pull_requests["vietanh910/AGENT"] = [
            {"number": 42, "title": "Add auth", "state": "open", "merged": False}
        ]

        # 1. Gọi merge khi policy_ok=False -> BỊ TỪ CHỐI (GitHubPolicyError) và ghi audit REJECTED
        with pytest.raises(GitHubPolicyError) as exc_info:
            merge_pull_request(
                client=fake_client,
                owner="vietanh910",
                repo="AGENT",
                pull_number=42,
                policy_ok=False,
                session=db_session,
                run_id="RUN-POL-01",
                task_id="TASK-GATE-01",
                actor="orchestrator",
            )
        assert "policy_ok=False" in str(exc_info.value)

        # Kiểm tra Audit Log đã ghi nhận REJECTED
        audit_entry = db_session.query(AuditLogModel).filter_by(run_id="RUN-POL-01").first()
        assert audit_entry is not None
        assert audit_entry.result == "REJECTED"
        assert audit_entry.action == "github_merge_pull_request"

        # 2. Gọi merge khi policy_ok=True -> THÀNH CÔNG và ghi audit SUCCESS
        res = merge_pull_request(
            client=fake_client,
            owner="vietanh910",
            repo="AGENT",
            pull_number=42,
            policy_ok=True,
            session=db_session,
            run_id="RUN-POL-02",
            task_id="TASK-DEVOPS-MERGE",
            actor="orchestrator",
        )
        assert res["merged"] is True

        audit_success = db_session.query(AuditLogModel).filter_by(run_id="RUN-POL-02").first()
        assert audit_success is not None
        assert audit_success.result == "SUCCESS"


class TestDevTesterEndToEndWorkflow:
    """Kiểm tra luồng phối hợp giữa DEV (tạo branch, commit, push, mở PR) và TESTER (checkout PR, test)."""

    def test_dev_commits_and_opens_pr_and_tester_verifies(self, test_git_env: dict[str, Path]):
        fake_github = FakeGitHubClient()
        remote_url = test_git_env["remote_url"]

        # 1. DEV lập trình và mở PR
        dev_req = TaskRequest(
            task_id="TASK-DEV-FEAT-10",
            project_id="RUN-E2E-01",
            agent_id="dev-01",
            role=AgentRole.DEV,
            action="implement_feature",
            instruction="Implement product catalog module",
        )

        dev_result = execute_dev_coding_action(
            request=dev_req,
            workspace_dir=test_git_env["dev_ws"],
            remote_url=remote_url,
            files_to_write={
                "src/catalog.py": "def get_products():\n    return [{'id': 1, 'name': 'Item A'}]\n",
                "tests/test_catalog.py": "from src.catalog import get_products\n\ndef test_catalog():\n    assert len(get_products()) == 1\n",
            },
            github_client=fake_github,
            owner="vietanh910",
            repo="AGENT",
        )

        assert dev_result.status == TaskStatus.COMPLETED
        assert len(dev_result.artifacts) == 1
        source_art = dev_result.artifacts[0]
        assert source_art.type == ArtifactType.SOURCE_CODE
        assert source_art.uri.startswith("github://")
        assert "commit_sha" in source_art.metadata

        # Kiểm tra PR đã được tạo trong fake_github
        prs = fake_github.list_pull_requests("vietanh910", "AGENT")
        assert len(prs) == 1
        assert prs[0]["number"] == 1
        assert "TASK-DEV-FEAT-10" in prs[0]["title"]

        # 2. TESTER nhận SOURCE_CODE từ input_artifacts và checkout PR để kiểm thử
        tester_req = TaskRequest(
            task_id="TASK-TEST-VERIFY-10",
            project_id="RUN-E2E-01",
            agent_id="tester-01",
            role=AgentRole.TESTER,
            action="run_test_suite",
            instruction="Run pytest on the pull request code",
            input_artifacts=[source_art],
        )

        tester_result = execute_tester_run_action(
            request=tester_req,
            workspace_dir=test_git_env["tester_ws"],
            remote_url=remote_url,
            test_command=["git", "status"],  # Lệnh kiểm tra an toàn trong môi trường test
        )

        assert tester_result.status == TaskStatus.COMPLETED
        assert len(tester_result.artifacts) == 1
        report_art = tester_result.artifacts[0]
        assert report_art.type == ArtifactType.TEST_REPORT
        assert report_art.metadata["verdict"] == "PASS"
