try:
    from agents.devops.tools.terminal import ExecutionResult, TerminalExecutor
except ImportError:
    from tools.terminal import ExecutionResult, TerminalExecutor


def docker_build(
    tag: str,
    path: str = ".",
    dockerfile: str | None = None,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    timeout: int = 300,
) -> ExecutionResult:
    """Thực hiện đóng gói Docker image thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor.")
    cmd = ["docker", "build", "-t", tag]
    if dockerfile:
        cmd.extend(["-f", dockerfile])
    cmd.append(path)
    return terminal.run(cmd, execute=execute, timeout=timeout)


def docker_run(
    image: str,
    name: str | None = None,
    ports: list[str] | None = None,
    detach: bool = True,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    timeout: int = 60,
) -> ExecutionResult:
    """Khởi chạy Docker container thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor.")
    cmd = ["docker", "run"]
    if detach:
        cmd.append("-d")
    if name:
        cmd.extend(["--name", name])
    if ports:
        for p in ports:
            cmd.extend(["-p", p])
    cmd.append(image)
    return terminal.run(cmd, execute=execute, timeout=timeout)


def docker_compose(
    subcommand: str = "up",
    args: list[str] | None = None,
    compose_file: str | None = None,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    timeout: int = 120,
) -> ExecutionResult:
    """Thực hiện docker compose thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor.")
    cmd = ["docker", "compose"]
    if compose_file:
        cmd.extend(["-f", compose_file])
    cmd.append(subcommand)
    if args:
        cmd.extend(args)
    return terminal.run(cmd, execute=execute, timeout=timeout)


def docker_stop(
    container: str,
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
) -> ExecutionResult:
    """Dừng Docker container thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor.")
    cmd = ["docker", "stop", container]
    return terminal.run(cmd, execute=execute)
