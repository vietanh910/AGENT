"""agents/devops/tools/deployment.py - Công cụ triển khai dịch vụ Staging và Health Check."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import httpx

try:
    from agents.devops.schemas import DeploymentReport, HealthCheckReport
    from agents.devops.tools.terminal import SecurityViolationError, TerminalExecutor
except ImportError:
    from schemas import DeploymentReport, HealthCheckReport
    from tools.terminal import SecurityViolationError, TerminalExecutor

logger = logging.getLogger(__name__)


def deploy(
    service_name: str,
    environment: str,
    image_tag: str,
    rollback_plan: list[str],
    terminal: TerminalExecutor | None = None,
    execute: bool = False,
    run_id: Optional[str] = None,
    staging_manager=None,
    container_port: int = 8000,
    env_vars: Optional[Dict[str, str]] = None,
    allow_prod_deploy: bool = False,
    gate_approved: bool = False,
) -> DeploymentReport:
    """Thực hiện quy trình triển khai dịch vụ thông qua TerminalExecutor."""
    if terminal is None:
        raise ValueError("Yêu cầu TerminalExecutor.")

    env_norm = environment.lower().strip()
    if env_norm in ["prod", "production"]:
        if not allow_prod_deploy or not gate_approved:
            raise SecurityViolationError(
                f"Triển khai lên môi trường '{environment}' bị chặn! "
                f"Yêu cầu ALLOW_PROD_DEPLOY=true và cổng approve_prod đã được phê duyệt (PERMISSION_DENIED)."
            )

    if not rollback_plan or len(rollback_plan) == 0:
        raise ValueError("Kế hoạch triển khai bắt buộc phải có rollback_plan.")

    deployed_url = f"http://127.0.0.1:8080"
    logs_tail = ""
    status_str = "DRY_RUN"

    if env_norm == "staging" and staging_manager and run_id:
        # Chuẩn bị stack cách ly tại deploy/staging/<run_id>/
        staging_dir, port = staging_manager.prepare_staging_stack(
            run_id=run_id,
            image_tag=image_tag,
            container_port=container_port,
            env_vars=env_vars,
            terminal=terminal,
            execute=execute,
        )
        deployed_url = f"http://127.0.0.1:{port}"

        # Chạy docker compose up -d qua TerminalExecutor
        exec_res = staging_manager.deploy_stack(
            run_id=run_id,
            terminal=terminal,
            execute=execute,
        )
        status_str = "DRY_RUN" if exec_res.dry_run else "SUCCESS"
        logs_tail = exec_res.stdout[:500]

    else:
        # Triển khai thông thường
        cmd = ["docker", "compose", "up", "-d"]
        exec_res = terminal.run(cmd, execute=execute)
        status_str = "DRY_RUN" if exec_res.dry_run else "SUCCESS"
        logs_tail = exec_res.stdout[:500]

    return DeploymentReport(
        service_name=service_name,
        environment=environment,
        version=image_tag,
        status=status_str,
        rollback_plan=rollback_plan,
        deployed_url=deployed_url,
        logs_tail=logs_tail,
    )


def health_check(
    endpoint: str,
    expected_status: int = 200,
    timeout: float = 5.0,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    dry_run: bool = True,
) -> HealthCheckReport:
    """Kiểm tra sức khỏe dịch vụ bằng HTTP request với timeout và retry."""
    if dry_run:
        return HealthCheckReport(
            endpoint=endpoint,
            status_code=expected_status,
            status="HEALTHY",
            latency_ms=15.0,
            response_body_sample='{"status": "ok", "dry_run": true}',
        )

    last_error = ""
    last_status = 500
    start_time = time.perf_counter()

    for attempt in range(1, max_retries + 1):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(endpoint)
                latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
                is_healthy = (resp.status_code == expected_status) or (200 <= resp.status_code < 300)
                sample = resp.text[:200] if resp.text else ""

                if is_healthy:
                    return HealthCheckReport(
                        endpoint=endpoint,
                        status_code=resp.status_code,
                        status="HEALTHY",
                        latency_ms=latency_ms,
                        response_body_sample=sample,
                    )
                else:
                    last_status = resp.status_code
                    last_error = f"HTTP {resp.status_code}: {sample}"
        except Exception as e:
            last_error = str(e)
            last_status = 500

        if attempt < max_retries:
            time.sleep(retry_delay)

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
    return HealthCheckReport(
        endpoint=endpoint,
        status_code=last_status,
        status="UNHEALTHY",
        latency_ms=latency_ms,
        response_body_sample=f"Health check failed after {max_retries} attempts: {last_error}",
    )
