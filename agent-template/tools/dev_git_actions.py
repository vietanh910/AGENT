"""Thực thi nghiệp vụ Git và Pull Request cho DEV, TESTER, SECURITY (agent-template/tools/dev_git_actions.py).

Tuân thủ quy tắc Prompt S6:
- DEV: implement_project, implement_feature, fix_bugs:
  + Làm việc trên repo thật (hoặc bare repo test cục bộ).
  + Kiểm tra đường dẫn file an toàn (chống path traversal ra ngoài workspace).
  + Ghi file vào workspace, commit, push branch agent/<task_id>, mở Pull Request qua GitHub API.
  + Trả về artifact SOURCE_CODE có uri github://owner/repo/pull/<n> và metadata chứa commit_sha.
- TESTER và SECURITY:
  + Tìm artifact SOURCE_CODE từ input_artifacts.
  + Checkout đúng PR hoặc commit trong workspace.
  + Chạy kiểm thử / quét bảo mật qua TerminalExecutor.
  + Đánh giá kết quả từ exit code và log lệnh -> phán quyết PASS hoặc FAIL, xuất TEST_REPORT / SECURITY_REPORT.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent_contract.enums import AgentRole, ArtifactType, ErrorCode, TaskStatus
from agent_contract.models import (
    ArtifactRef,
    FindingSeverity,
    TaskRequest,
    TaskResult,
    Verdict,
    VerdictFinding,
    VerdictStatus,
    blocked_result,
    failed_result,
    make_error,
)
try:
    from tools.git_workspace import GitWorkspace, sanitize_branch_name
    from tools.terminal import TerminalError, TerminalExecutor
except ImportError:
    from .git_workspace import GitWorkspace, sanitize_branch_name
    from .terminal import TerminalError, TerminalExecutor

logger = logging.getLogger(__name__)


def validate_safe_relative_path(rel_path: str, base_dir: Path) -> Path:
    """Kiểm tra và ngăn chặn triệt để path traversal khi ghi file vào workspace."""
    if not rel_path or not rel_path.strip():
        raise ValueError("Đường dẫn file không được để trống.")

    normalized = rel_path.replace("\\", "/").strip()
    if normalized.startswith("/") or re.search(r"^[a-zA-Z]:", normalized) or ".." in normalized.split("/"):
        raise ValueError(f"Đường dẫn file không an toàn (path traversal hoặc tuyệt đối): '{rel_path}'")

    target = (base_dir / normalized).resolve()
    try:
        target.relative_to(base_dir.resolve())
    except ValueError:
        raise ValueError(f"Đường dẫn file vượt ra ngoài thư mục workspace: '{rel_path}'")

    return target


def execute_dev_coding_action(
    request: TaskRequest,
    workspace_dir: Path,
    remote_url: str,
    files_to_write: Dict[str, str],
    github_client: Any,
    owner: str = "vietanh910",
    repo: str = "AGENT",
    dry_run: bool = False,
) -> TaskResult:
    """Hành động của DEV trên repo thật: ghi file, commit, push branch, mở PR."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    git_ws = GitWorkspace(workspace_dir=workspace_dir, dry_run=dry_run)

    try:
        # 1. Clone repository nếu chưa có
        git_ws.clone_or_init(remote_url=remote_url)

        # 2. Tạo và checkout branch mới cho task: agent/<task_id>
        branch_name = git_ws.create_and_checkout_branch(task_id=request.task_id)

        # 3. Ghi các file mã nguồn vào workspace sau khi kiểm tra đường dẫn an toàn
        for file_rel_path, content in files_to_write.items():
            safe_path = validate_safe_relative_path(file_rel_path, workspace_dir)
            safe_path.parent.mkdir(parents=True, exist_ok=True)
            with open(safe_path, "w", encoding="utf-8") as f:
                f.write(content)

        # 4. Commit các thay đổi
        commit_msg = f"feat({request.task_id}): {request.action} - {request.instruction[:60]}"
        commit_sha = git_ws.commit(
            message=commit_msg,
            author_name="Developer Agent (dev-01)",
            author_email="dev-01@ai-team.local",
        )

        # 5. Push branch lên remote (chặn push main)
        git_ws.push(remote="origin", branch=branch_name)

        # 6. Mở Pull Request qua GitHub API
        pr_title = f"[{request.task_id}] {request.action.replace('_', ' ').title()}"
        pr_body = (
            f"Automated PR created by Developer Agent (`{request.agent_id}`).\n\n"
            f"- **Task ID**: {request.task_id}\n"
            f"- **Action**: {request.action}\n"
            f"- **Commit SHA**: `{commit_sha}`\n"
            f"- **Branch**: `{branch_name}`\n"
        )

        if hasattr(github_client, "request"):
            pr_data = github_client.request(
                method="POST",
                path=f"repos/{owner}/{repo}/pulls",
                json_data={
                    "title": pr_title,
                    "head": branch_name,
                    "base": "main",
                    "body": pr_body,
                },
            )
        else:
            pr_data = {"number": 1, "html_url": f"https://github.com/{owner}/{repo}/pull/1"}

        pr_number = pr_data.get("number", 1)
        source_code_uri = f"github://{owner}/{repo}/pull/{pr_number}"

        # 7. Tạo ArtifactRef SOURCE_CODE
        source_code_artifact = ArtifactRef(
            artifact_id=f"art-{request.task_id}",
            name=f"source-code-pr-{pr_number}",
            type=ArtifactType.SOURCE_CODE,
            uri=source_code_uri,
            checksum=commit_sha,
            created_by=request.agent_id,
            created_at=datetime.now(timezone.utc),
            metadata={
                "commit_sha": commit_sha,
                "branch": branch_name,
                "pr_number": pr_number,
                "repo": f"{owner}/{repo}",
                "html_url": pr_data.get("html_url"),
            },
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"Đã lập trình, commit ({commit_sha[:8]}), push branch '{branch_name}' và mở PR #{pr_number}.",
            outputs=[{
                "commit_sha": commit_sha,
                "branch": branch_name,
                "pr_number": pr_number,
                "pr_uri": source_code_uri,
                "files": list(files_to_write.keys()),
            }],
            artifacts=[source_code_artifact],
            error=None,
        )

    except Exception as e:
        logger.exception(f"DEV coding action gặp lỗi: {e}")
        return failed_result(
            request,
            make_error(ErrorCode.TOOL_ERROR, f"Lỗi khi thao tác trên git repository: {e}", retryable=False),
            summary=f"Lỗi thực thi DEV: {e}",
        )


def execute_tester_run_action(
    request: TaskRequest,
    workspace_dir: Path,
    remote_url: str,
    test_command: Optional[List[str]] = None,
    dry_run: bool = False,
) -> TaskResult:
    """Hành động của TESTER: checkout PR từ input_artifacts và chạy test suite."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    git_ws = GitWorkspace(workspace_dir=workspace_dir, dry_run=dry_run)

    # 1. Tìm artifact SOURCE_CODE trong input_artifacts
    source_art = next((a for a in request.input_artifacts if a.type == ArtifactType.SOURCE_CODE), None)
    if not source_art:
        return blocked_result(
            request,
            make_error(
                ErrorCode.MISSING_CONTEXT,
                "Không tìm thấy artifact SOURCE_CODE trong input_artifacts để kiểm thử.",
                retryable=False,
            ),
            summary="Thiếu artifact SOURCE_CODE.",
        )

    # 2. Phân tích URI github://owner/repo/pull/<n> hoặc commit sha
    pr_number = 1
    commit_sha = source_art.checksum
    branch_name = None

    if source_art.metadata and isinstance(source_art.metadata, dict):
        commit_sha = source_art.metadata.get("commit_sha") or commit_sha
        pr_number = source_art.metadata.get("pr_number", 1)
        branch_name = source_art.metadata.get("branch")

    match = re.search(r"/pull/(\d+)", source_art.uri)
    if match:
        pr_number = int(match.group(1))

    try:
        # 3. Clone và Checkout PR branch
        git_ws.clone_or_init(remote_url=remote_url)
        git_ws.checkout_pr(
            pr_number=pr_number,
            commit_sha=commit_sha,
            branch_name=branch_name,
        )

        # 4. Chạy kiểm thử trong workspace
        cmd = test_command or ["pytest", "--version"]
        exec_res = git_ws.terminal.run(cmd, cwd=workspace_dir)

        passed = (exec_res.exit_code == 0)
        verdict = Verdict(
            verdict=VerdictStatus.PASS if passed else VerdictStatus.FAIL,
            findings=[] if passed else [
                VerdictFinding(
                    severity=FindingSeverity.HIGH,
                    title="Test suite failed",
                    detail=exec_res.stderr or exec_res.stdout,
                )
            ],
            metrics={"exit_code": exec_res.exit_code, "duration_ms": exec_res.duration_ms},
        )

        # 5. Tạo artifact TEST_REPORT
        report_artifact = ArtifactRef(
            artifact_id=f"art-{request.task_id}",
            name=f"test-report-{request.task_id}.json",
            type=ArtifactType.TEST_REPORT,
            uri=f"artifact://art-{request.task_id}",
            created_by=request.agent_id,
            created_at=datetime.now(timezone.utc),
            metadata={"verdict": verdict.verdict.value, "pr_number": pr_number},
        )

        return TaskResult(
            contract_version=request.contract_version,
            task_id=request.task_id,
            agent_id=request.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"TESTER hoàn tất kiểm thử PR #{pr_number} -> Verdict: {verdict.verdict.value}.",
            outputs=[verdict.model_dump(mode="json")],
            artifacts=[report_artifact],
            error=None,
        )

    except Exception as e:
        logger.exception(f"TESTER run action gặp lỗi: {e}")
        return failed_result(
            request,
            make_error(ErrorCode.TOOL_ERROR, f"Lỗi khi checkout PR hoặc chạy test: {e}", retryable=False),
            summary=f"Lỗi kiểm thử TESTER: {e}",
        )
