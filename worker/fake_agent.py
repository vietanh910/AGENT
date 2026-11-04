"""Worker giả lập (Fake Agent) phục vụ kiểm thử và mô phỏng hệ thống (không cần LLM).

Hỗ trợ các hành vi cấu hình linh hoạt theo từng action hoặc từng task:
1. Thành công sau N giây (delay_seconds), tạo kèm mock ArtifactRef.
2. Lỗi retryable N lần rồi thành công (RETRYABLE_THEN_SUCCESS).
3. Lỗi không retryable (NON_RETRYABLE_ERROR) -> kết thúc ngay.
4. Bị chặn (BLOCKED) với mã lỗi MISSING_CONTEXT.
5. Treo mãi (HANG) để thử nghiệm cơ chế deadline timeout và cancellation.
6. Sập tiến trình / Ngoại lệ bất ngờ (CRASH).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from agent_contract.enums import AgentRole, ArtifactType, ErrorCode, TaskStatus
from agent_contract.models import (
    AgentError,
    AgentIdentity,
    ArtifactRef,
    TaskRequest,
    TaskResult,
    blocked_result,
    failed_result,
    make_error,
)

logger = logging.getLogger(__name__)


ACTION_OUTPUT_ARTIFACT_TYPES: Dict[str, ArtifactType] = {
    "create_plan": ArtifactType.PLAN,
    "define_backlog": ArtifactType.BACKLOG,
    "write_requirements": ArtifactType.REQUIREMENT,
    "write_user_stories": ArtifactType.USER_STORY,
    "design_architecture": ArtifactType.ARCHITECTURE,
    "design_schema": ArtifactType.DATABASE_SCHEMA,
    "create_test_cases": ArtifactType.TEST_CASE,
    "implement_project": ArtifactType.SOURCE_CODE,
    "implement_feature": ArtifactType.SOURCE_CODE,
    "fix_bugs": ArtifactType.SOURCE_CODE,
    "run_test_suite": ArtifactType.TEST_REPORT,
    "security_review": ArtifactType.SECURITY_REPORT,
    "generate_ci_config": ArtifactType.DOCUMENTATION,
    "build_image": ArtifactType.DEPLOYMENT,
    "deploy": ArtifactType.DEPLOYMENT,
    "health_check": ArtifactType.DOCUMENTATION,
    "accept_release": ArtifactType.DOCUMENTATION,
    "run_sprint_review": ArtifactType.DOCUMENTATION,
    "run_retrospective": ArtifactType.DOCUMENTATION,
    "report_status": ArtifactType.PLAN,
}


class FakeBehavior(str, Enum):
    """Các chế độ hành vi của FakeAgent."""
    SUCCESS = "SUCCESS"
    RETRYABLE_THEN_SUCCESS = "RETRYABLE_THEN_SUCCESS"
    NON_RETRYABLE_ERROR = "NON_RETRYABLE_ERROR"
    BLOCKED = "BLOCKED"
    HANG = "HANG"
    CRASH = "CRASH"


@dataclass
class BehaviorConfig:
    """Cấu hình hành vi cho một action hoặc task."""
    behavior: FakeBehavior = FakeBehavior.SUCCESS
    delay_seconds: float = 0.05
    fail_attempts_count: int = 1
    error_code: ErrorCode = ErrorCode.TOOL_ERROR
    error_message: str = "Simulated error from FakeAgent"
    retryable: bool = True
    artifact_name: str = "mock_output.txt"
    artifact_type: ArtifactType = ArtifactType.DOCUMENTATION


class FakeAgent:
    """Agent giả lập tương thích với giao diện BaseAgent.handle()."""

    def __init__(
        self,
        agent_id: str,
        role: AgentRole,
        default_behavior: Optional[BehaviorConfig] = None,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.default_behavior = default_behavior or BehaviorConfig()
        self._action_configs: Dict[str, BehaviorConfig] = {}
        self._task_configs: Dict[str, BehaviorConfig] = {}
        self._task_attempt_counts: Dict[str, int] = {}
        self._lock = threading.Lock()

    def configure_action(self, action: str, config: BehaviorConfig) -> None:
        """Cấu hình hành vi cụ thể cho một action."""
        with self._lock:
            self._action_configs[action] = config

    def configure_task(self, task_id: str, config: BehaviorConfig) -> None:
        """Cấu hình hành vi cụ thể cho một task_id."""
        with self._lock:
            self._task_configs[task_id] = config

    def get_config(self, task_id: str, action: str) -> BehaviorConfig:
        """Lấy cấu hình hành vi ưu tiên task -> action -> mặc định."""
        with self._lock:
            if task_id in self._task_configs:
                return self._task_configs[task_id]
            if action in self._action_configs:
                return self._action_configs[action]
            return self.default_behavior

    def reset(self) -> None:
        """Đặt lại toàn bộ cấu hình tạm thời về mặc định."""
        with self._lock:
            self._action_configs.clear()
            self._task_configs.clear()
            self._task_attempt_counts.clear()

    def handle(
        self,
        request: TaskRequest,
        cancel_event: Optional[threading.Event] = None,
    ) -> TaskResult:
        """Xử lý TaskRequest theo cấu hình hành vi đã định nghĩa."""
        start_time = time.perf_counter()
        config = self.get_config(request.task_id, request.action)

        with self._lock:
            current_attempts = self._task_attempt_counts.get(request.task_id, 0) + 1
            self._task_attempt_counts[request.task_id] = current_attempts

        # 1. Hành vi SẬP TIẾN TRÌNH / CRASH
        if config.behavior == FakeBehavior.CRASH:
            raise RuntimeError(f"FakeAgent '{self.agent_id}' crashed as requested.")

        # 2. Hành vi TREO MÃI (HANG)
        if config.behavior == FakeBehavior.HANG:
            logger.info(f"FakeAgent '{self.agent_id}' bắt đầu HANG cho task '{request.task_id}'.")
            while True:
                if cancel_event and cancel_event.is_set():
                    logger.info(f"FakeAgent '{self.agent_id}' nhận được cờ hủy khi đang HANG.")
                    return failed_result(
                        request,
                        make_error(ErrorCode.UNKNOWN_ERROR, "Task was cancelled while hanging.", retryable=False),
                        summary="Task cancelled.",
                    )
                time.sleep(0.1)

        # 3. Trì hoãn thời gian có kiểm tra cờ hủy
        if config.delay_seconds > 0:
            elapsed = 0.0
            step = 0.05
            while elapsed < config.delay_seconds:
                if cancel_event and cancel_event.is_set():
                    return failed_result(
                        request,
                        make_error(ErrorCode.UNKNOWN_ERROR, "Task was cancelled during delay.", retryable=False),
                        summary="Task cancelled.",
                    )
                sleep_time = min(step, config.delay_seconds - elapsed)
                time.sleep(sleep_time)
                elapsed += sleep_time

        # 4. Hành vi BỊ CHẶN (BLOCKED)
        if config.behavior == FakeBehavior.BLOCKED:
            return blocked_result(
                request,
                reason="Yêu cầu bổ sung tài liệu kiến trúc hoặc biến môi trường còn thiếu.",
                code=ErrorCode.MISSING_CONTEXT,
            )

        # 5. Hành vi LỖI KHÔNG RETRYABLE
        if config.behavior == FakeBehavior.NON_RETRYABLE_ERROR:
            return failed_result(
                request,
                make_error(
                    code=config.error_code,
                    message=config.error_message,
                    retryable=False,
                ),
                summary="Lỗi không thể thử lại.",
            )

        # 6. Hành vi RETRYABLE THỬ N LẦN RỒI THÀNH CÔNG
        if config.behavior == FakeBehavior.RETRYABLE_THEN_SUCCESS:
            if current_attempts <= config.fail_attempts_count:
                return failed_result(
                    request,
                    make_error(
                        code=config.error_code,
                        message=f"{config.error_message} (Lần thử {current_attempts}/{config.fail_attempts_count})",
                        retryable=True,
                    ),
                    summary=f"Lỗi thử lại lần {current_attempts}.",
                )
            # Sau N lần thì rơi xuống thành công

        # 7. Hành vi THÀNH CÔNG (SUCCESS)
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        art_type = config.artifact_type
        if art_type == ArtifactType.DOCUMENTATION and request.action in ACTION_OUTPUT_ARTIFACT_TYPES:
            art_type = ACTION_OUTPUT_ARTIFACT_TYPES[request.action]

        mock_artifact = ArtifactRef(
            artifact_id=f"art-{request.task_id}",
            created_by=self.agent_id,
            uri=f"artifact://mock-{request.task_id}",
            name=config.artifact_name,
            type=art_type,
            version="1.0.0",
            checksum="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            created_at=datetime.now(timezone.utc),
        )

        return TaskResult(
            task_id=request.task_id,
            agent_id=self.agent_id,
            status=TaskStatus.COMPLETED,
            summary=f"FakeAgent hoàn thành action '{request.action}' thành công.",
            artifacts=[mock_artifact],
            outputs=[{"result": f"Execution output for {request.action}", "duration_ms": duration_ms}],
            error=None,
        )
