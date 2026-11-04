try:
    from agents.devops.tools.terminal import ExecutionResult, TerminalExecutor
except ImportError:
    from tools.terminal import ExecutionResult, TerminalExecutor


def git_clone(
    repo_url: str,
    target_dir: str | None = None,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    timeout: int = 120,
) -> ExecutionResult:
    """Thực hiện lệnh git clone thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor để thực thi lệnh git.")
    cmd = ["git", "clone", repo_url]
    if target_dir:
        cmd.append(target_dir)
    return terminal.run(cmd, execute=execute, timeout=timeout)


def git_pull(
    remote: str = "origin",
    branch: str = "main",
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    timeout: int = 60,
) -> ExecutionResult:
    """Thực hiện lệnh git pull thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor để thực thi lệnh git.")
    cmd = ["git", "pull", remote, branch]
    return terminal.run(cmd, execute=execute, timeout=timeout)


def git_checkout(
    branch: str,
    create: bool = False,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
) -> ExecutionResult:
    """Thực hiện lệnh git checkout thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor để thực thi lệnh git.")
    cmd = ["git", "checkout", "-b", branch] if create else ["git", "checkout", branch]
    return terminal.run(cmd, execute=execute)


def git_commit(
    message: str,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
) -> ExecutionResult:
    """Thực hiện lệnh git commit thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor để thực thi lệnh git.")
    cmd = ["git", "commit", "-m", message]
    return terminal.run(cmd, execute=execute)


def git_push(
    remote: str = "origin",
    branch: str = "main",
    force: bool = False,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
) -> ExecutionResult:
    """Thực hiện lệnh git push thông qua TerminalExecutor (chặn cờ force nguy hiểm)."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor để thực thi lệnh git.")
    cmd = ["git", "push"]
    if force:
        cmd.append("--force")  # Sẽ bị TerminalExecutor chặn bởi DANGEROUS_PATTERNS
    cmd.extend([remote, branch])
    return terminal.run(cmd, execute=execute)
