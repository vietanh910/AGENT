"""Công cụ Git Workspace (agent-template/tools/git_workspace.py) dùng chung cho DEV, TESTER, SECURITY.

Quy tắc bảo mật và nghiệp vụ:
- Thao tác git hoàn toàn thông qua TerminalExecutor (không gọi shell, whitelist an toàn).
- Tuyệt đối KHÔNG force push (--force, -f).
- Tuyệt đối KHÔNG push lên branch main hoặc master.
- Tên branch tự động được làm sạch theo chuẩn: agent/<task_id>.
- Tôn trọng cờ DRY_RUN (không ghi/push khi dry_run=True).
- Hỗ trợ repository remote dạng URL internet hoặc file:// (bare git repo cục bộ để test offline).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from tools.terminal import SecurityViolationError, TerminalError, TerminalExecutor
except ImportError:
    from .terminal import SecurityViolationError, TerminalError, TerminalExecutor

logger = logging.getLogger(__name__)


def sanitize_branch_name(task_id: str) -> str:
    """Làm sạch task_id để tạo tên git branch hợp lệ và an toàn."""
    if not task_id or not task_id.strip():
        return "agent/task-unknown"
    # Giữ chữ cái, số, gạch ngang, gạch dưới, chấm
    cleaned = re.sub(r"[^\w\.\-]", "-", task_id.strip()).strip(".-")
    return f"agent/{cleaned}"


class GitWorkspace:
    """Quản lý workspace git cho các Agent phát triển và kiểm thử."""

    def __init__(
        self,
        workspace_dir: Path | str,
        github_token: Optional[str] = None,
        dry_run: bool = False,
        terminal: Optional[TerminalExecutor] = None,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.github_token = github_token
        self.dry_run = dry_run
        if terminal is not None:
            self.terminal = terminal
            parent_dir = self.workspace_dir.parent
            if hasattr(self.terminal, "allowed_directories") and parent_dir not in self.terminal.allowed_directories:
                self.terminal.allowed_directories.append(parent_dir)
        else:
            self.terminal = TerminalExecutor(
                workspace_dir=self.workspace_dir,
                allowed_directories=[self.workspace_dir, self.workspace_dir.parent],
                allowed_commands=["git", "pytest", "python", "npm", "mvn"],
                dry_run=dry_run,
            )

    def clone_or_init(self, remote_url: str, branch: str = "main") -> None:
        """Clone repository từ remote_url (hỗ trợ cả URL file:// cho bare repo cục bộ)."""
        git_dir = self.workspace_dir / ".git"
        if git_dir.exists():
            logger.info(f"Workspace '{self.workspace_dir}' đã có git repo. Đang cập nhật...")
            try:
                self.terminal.run(["git", "fetch", "origin"], cwd=self.workspace_dir)
            except Exception as e:
                logger.debug(f"Không thể fetch origin: {e}")
            return

        # Clone repository vào thư mục workspace
        logger.info(f"Đang clone repository từ '{remote_url}' vào '{self.workspace_dir}'...")
        env = {}
        if self.github_token:
            env["GITHUB_TOKEN"] = self.github_token

        # Chạy clone từ thư mục cha hoặc clone trực tiếp vào workspace
        parent_dir = self.workspace_dir.parent
        self.terminal.run(
            ["git", "clone", remote_url, str(self.workspace_dir)],
            cwd=parent_dir,
            env=env,
        )

    def create_and_checkout_branch(self, task_id: str) -> str:
        """Tạo branch mới cho task theo định dạng agent/<task_id> và chuyển sang branch đó."""
        branch_name = sanitize_branch_name(task_id)
        logger.info(f"Tạo và checkout branch '{branch_name}' trong workspace...")

        # Thử checkout branch nếu đã có, nếu chưa thì tạo mới với cờ -b
        try:
            self.terminal.run(["git", "checkout", "-b", branch_name], cwd=self.workspace_dir)
        except Exception:
            # Nếu branch đã tồn tại
            self.terminal.run(["git", "checkout", branch_name], cwd=self.workspace_dir)

        return branch_name

    def commit(
        self,
        message: str,
        author_name: str = "Developer Agent",
        author_email: str = "dev@ai-team.local",
    ) -> str:
        """Thêm thay đổi vào staging và tạo commit, trả về mã commit SHA."""
        logger.info(f"Commit thay đổi vào git: '{message}'...")

        self.terminal.run(["git", "config", "user.name", author_name], cwd=self.workspace_dir)
        self.terminal.run(["git", "config", "user.email", author_email], cwd=self.workspace_dir)
        self.terminal.run(["git", "add", "-A"], cwd=self.workspace_dir)

        # Kiểm tra nếu không có thay đổi nào để commit
        status_res = self.terminal.run(["git", "status", "--porcelain"], cwd=self.workspace_dir)
        if not status_res.stdout.strip():
            logger.info("Không có thay đổi mới nào để commit.")
            sha_res = self.terminal.run(["git", "rev-parse", "HEAD"], cwd=self.workspace_dir)
            return sha_res.stdout.strip()

        self.terminal.run(["git", "commit", "-m", message], cwd=self.workspace_dir)
        sha_res = self.terminal.run(["git", "rev-parse", "HEAD"], cwd=self.workspace_dir)
        return sha_res.stdout.strip()

    def get_current_branch(self) -> str:
        """Lấy tên branch hiện tại."""
        res = self.terminal.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.workspace_dir)
        return res.stdout.strip()

    def push(self, remote: str = "origin", branch: Optional[str] = None) -> None:
        """Push branch lên remote. TUYỆT ĐỐI CHẶN PUSH MAIN/MASTER HOẶC FORCE PUSH."""
        target_branch = branch or self.get_current_branch()

        # Kiểm tra cấm push lên main hoặc master
        clean_target = target_branch.lower().replace("refs/heads/", "").strip()
        if clean_target in ("main", "master"):
            raise SecurityViolationError(
                f"Tác vụ Agent TUYỆT ĐỐI BỊ CẤM push trực tiếp lên branch '{clean_target}'! Phải qua Pull Request."
            )

        if self.dry_run:
            logger.info(f"[DRY-RUN] Giả lập: Bỏ qua git push {remote} {target_branch}.")
            return

        logger.info(f"Push branch '{target_branch}' lên remote '{remote}'...")
        env = {}
        if self.github_token:
            env["GITHUB_TOKEN"] = self.github_token

        self.terminal.run(["git", "push", "-u", remote, target_branch], cwd=self.workspace_dir, env=env)

    def checkout_pr(
        self,
        pr_number: int,
        commit_sha: Optional[str] = None,
        branch_name: Optional[str] = None,
        remote: str = "origin",
    ) -> None:
        """Checkout code của một Pull Request để TESTER hoặc SECURITY kiểm tra."""
        logger.info(f"Đang checkout PR #{pr_number} (commit_sha={commit_sha}, branch={branch_name})...")

        if commit_sha:
            try:
                self.terminal.run(["git", "fetch", remote], cwd=self.workspace_dir)
                self.terminal.run(["git", "checkout", commit_sha], cwd=self.workspace_dir)
                return
            except Exception as e:
                logger.debug(f"Không thể checkout thẳng commit sha {commit_sha}: {e}")

        # Thử checkout theo branch_name nếu có
        if branch_name:
            try:
                self.terminal.run(["git", "fetch", remote, branch_name], cwd=self.workspace_dir)
                self.terminal.run(["git", "checkout", branch_name], cwd=self.workspace_dir)
                return
            except Exception as e:
                logger.debug(f"Không thể checkout theo branch {branch_name}: {e}")

        # Thử fetch ref pull/<n>/head của GitHub
        pr_branch = f"pr-{pr_number}"
        try:
            self.terminal.run(
                ["git", "fetch", remote, f"pull/{pr_number}/head:{pr_branch}"],
                cwd=self.workspace_dir,
            )
            self.terminal.run(["git", "checkout", pr_branch], cwd=self.workspace_dir)
        except Exception as e:
            # Nếu là bare repo cục bộ không có ref pull/
            logger.warning(f"Không thể fetch PR ref pull/{pr_number}/head: {e}. Thử fetch all...")
            self.terminal.run(["git", "fetch", remote], cwd=self.workspace_dir)
            if commit_sha:
                self.terminal.run(["git", "checkout", commit_sha], cwd=self.workspace_dir)
