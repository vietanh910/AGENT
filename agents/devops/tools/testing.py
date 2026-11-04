try:
    from agents.devops.tools.terminal import ExecutionResult, TerminalExecutor
except ImportError:
    from tools.terminal import ExecutionResult, TerminalExecutor


def run_tests(
    test_path: str = "tests",
    extra_args: list[str] | None = None,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    timeout: int = 120,
) -> ExecutionResult:
    """Thực thi kiểm thử tự động với pytest thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor.")
    cmd = ["pytest", test_path]
    if extra_args:
        cmd.extend(extra_args)
    return terminal.run(cmd, execute=execute, timeout=timeout)
