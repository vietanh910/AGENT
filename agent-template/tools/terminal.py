"""Lớp thực thi lệnh hệ thống Terminal an toàn cho Agent (agent-template/tools/terminal.py).

Tuân thủ nghiêm ngặt:
- Whitelist các lệnh cho phép (ví dụ: git, pytest, python, npm...).
- Chặn tuyệt đối shell operators (&&, ||, ;, |, >, >>, <, `) để chống Command Injection.
- Cấm hoàn toàn: git push --force, git push -f, git push lên main/master, rm -rf, drop database...
- Che giấu Token, Secret, Password trong output log.
- Chạy qua subprocess.run không bật shell (shell=False).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

from pydantic import BaseModel


class TerminalError(Exception):
    """Ngoại lệ chung cho lớp terminal."""
    pass


class SecurityViolationError(TerminalError):
    """Vi phạm quy tắc an toàn (lệnh ngoài whitelist, mẫu nguy hiểm, shell operator, symlink)."""
    pass


class CommandTimeoutError(TerminalError):
    """Lệnh chạy quá thời gian timeout cho phép."""
    pass


class CommandExecutionError(TerminalError):
    """Lệnh chạy thất bại với mã lỗi (exit code khác 0)."""

    def __init__(
        self,
        message: str,
        exit_code: int,
        stdout: str,
        stderr: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.retryable = retryable


class ExecutionResult(BaseModel):
    """Kết quả thực thi một lệnh từ terminal."""

    command: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    dry_run: bool


# Danh sách các mẫu lệnh nguy hiểm bị cấm tuyệt đối
DANGEROUS_PATTERNS = [
    re.compile(r"\brm\s+-[a-zA-Z]*r[a-zA-Z]*f\b", re.IGNORECASE),
    re.compile(r"\brm\s+-[a-zA-Z]*f[a-zA-Z]*r\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*--force\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*-f\b", re.IGNORECASE),
    re.compile(r"\bgit\s+push\s+.*(?:\s+|origin\s+|upstream\s+)(?:main|master)\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+system\s+prune\b", re.IGNORECASE),
    re.compile(r"\bdocker\s+volume\s+prune\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+(database|table|schema)\b", re.IGNORECASE),
    re.compile(r"\b(curl|wget)\b.*\|\s*(sh|bash|zsh)\b", re.IGNORECASE),
    re.compile(r"\b(sudo|su|runas)\b", re.IGNORECASE),
    re.compile(r"\b(mkfs|dd\s+if=)\b", re.IGNORECASE),
]

SHELL_OPERATORS = ["&&", "||", "|", ";", ">", ">>", "<", "`", "$("]
MAX_OUTPUT_LENGTH = 50000


def mask_secrets(text: str, custom_env: dict[str, str] | None = None) -> str:
    """Che giấu giá trị của các secret trong văn bản stdout/stderr."""
    if not text:
        return ""

    masked = text
    all_envs = {**os.environ, **(custom_env or {})}
    for key, val in all_envs.items():
        upper_key = key.upper()
        if any(term in upper_key for term in ["KEY", "TOKEN", "SECRET", "PASSWORD"]):
            if val and len(val.strip()) >= 4:
                masked = masked.replace(val, "***REDACTED***")

    pattern = re.compile(
        r"(?i)\b(key|token|secret|password|api_key|bearer)[\s:=]+(['\"]?)([a-zA-Z0-9_\-\.]{6,})\2"
    )
    masked = pattern.sub(r"\1=***REDACTED***", masked)
    masked = re.sub(r"ghp_[a-zA-Z0-9]{20,}", "***REDACTED***", masked)
    masked = re.sub(r"gsk_[a-zA-Z0-9]{20,}", "***REDACTED***", masked)
    masked = re.sub(r"sk-[a-zA-Z0-9]{20,}", "***REDACTED***", masked)
    return masked


def truncate_output(text: str, max_chars: int = MAX_OUTPUT_LENGTH) -> str:
    """Giới hạn kích thước output để tránh tràn bộ nhớ."""
    if len(text) > max_chars:
        half = max_chars // 2
        return text[:half] + "\n\n[... NỘI DUNG ĐÃ ĐƯỢC CẮT BỚT DO VƯỢT QUÁ GIỚI HẠN ...]\n\n" + text[-half:]
    return text


class TerminalExecutor:
    """Thực thi lệnh an toàn cho Agent."""

    def __init__(
        self,
        workspace_dir: str | Path,
        allowed_commands: list[str] | None = None,
        dry_run: bool = False,
    ) -> None:
        self.workspace_dir = Path(workspace_dir).resolve()
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.allowed_commands = allowed_commands or ["git", "pytest", "python", "npm", "mvn", "docker"]
        self.dry_run = dry_run

    def _validate_command_safety(self, cmd_args: Sequence[str]) -> None:
        if not cmd_args:
            raise SecurityViolationError("Lệnh không được để trống.")

        base_cmd = Path(cmd_args[0]).name.lower()
        if base_cmd.endswith(".exe"):
            base_cmd = base_cmd[:-4]

        if base_cmd not in [c.lower() for c in self.allowed_commands]:
            raise SecurityViolationError(
                f"Lệnh '{base_cmd}' không nằm trong whitelist cho phép ({self.allowed_commands})."
            )

        cmd_line = " ".join(cmd_args)
        for pattern in DANGEROUS_PATTERNS:
            if pattern.search(cmd_line):
                raise SecurityViolationError(
                    f"Phát hiện mẫu lệnh nguy hiểm bị cấm: '{pattern.pattern}' trong '{cmd_line}'"
                )

        for arg in cmd_args:
            for op in SHELL_OPERATORS:
                if op in arg:
                    raise SecurityViolationError(
                        f"Phát hiện toán tử shell bị cấm '{op}' trong đối số: '{arg}'"
                    )

    def run(
        self,
        command: Sequence[str],
        timeout: int = 120,
        env: dict[str, str] | None = None,
        cwd: Path | str | None = None,
    ) -> ExecutionResult:
        """Thực thi một lệnh theo danh sách tham số (không bật shell)."""
        self._validate_command_safety(command)

        work_dir = Path(cwd).resolve() if cwd else self.workspace_dir

        if self.dry_run:
            cmd_name = Path(command[0]).name.lower()
            # Cho phép các lệnh đọc chạy bình thường trong dry_run
            is_read_only = any(
                sub in " ".join(command).lower()
                for sub in ["status", "diff", "log", "branch", "--version", "rev-parse"]
            )
            if not is_read_only:
                return ExecutionResult(
                    command=list(command),
                    exit_code=0,
                    stdout=f"[DRY-RUN] Giả lập thực thi: {' '.join(command)}",
                    stderr="",
                    duration_ms=0,
                    dry_run=True,
                )

        merged_env = {**os.environ, **(env or {})}
        start_time = time.perf_counter()

        try:
            proc = subprocess.run(
                command,
                cwd=str(work_dir),
                env=merged_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
            )
            duration_ms = int((time.perf_counter() - start_time) * 1000)

            stdout = mask_secrets(truncate_output(proc.stdout), env)
            stderr = mask_secrets(truncate_output(proc.stderr), env)

            if proc.returncode != 0:
                raise CommandExecutionError(
                    message=f"Lệnh '{command[0]}' thất bại với mã lỗi {proc.returncode}: {stderr or stdout}",
                    exit_code=proc.returncode,
                    stdout=stdout,
                    stderr=stderr,
                )

            return ExecutionResult(
                command=list(command),
                exit_code=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                duration_ms=duration_ms,
                dry_run=False,
            )

        except subprocess.TimeoutExpired as e:
            duration_ms = int((time.perf_counter() - start_time) * 1000)
            raise CommandTimeoutError(
                f"Lệnh '{command[0]}' chạy quá thời gian tối đa ({timeout} giây)."
            ) from e
