"""Kiểm thử tích hợp luồng Artifact trên Worker Host (worker/tests/test_artifact_workflow.py).

Kiểm tra:
1. Trước khi chạy: tải input_artifact uri artifact:// về agent/artifacts/inbox/, kiểm tra sha256.
   - Thành công: file nằm trong inbox, agent nhận task bình thường.
   - Thất bại (404 hoặc sai checksum): trả kết quả BLOCKED (MISSING_CONTEXT), không chạy task.
   - uri github://: không tải, agent tự dùng git.
2. Sau khi chạy: upload mọi artifact trong artifacts/index.json lên Artifact Store,
   cập nhật uri artifact://<id> vào TaskResult trước khi đưa vào outbox.
   - Upload thất bại: kết quả trả về không được coi là hoàn thành (FAILED retryable).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from agent_contract.enums import AgentRole, ArtifactType, ErrorCode, TaskStatus
from agent_contract.models import ArtifactRef, TaskResult
from agent_contract.system_models import DispatchRequest
from worker.fake_agent import FakeAgent
from worker.host.outbox import OutboxService
from worker.host.runner import AgentRunner, RunnerRegistry


@pytest.fixture
def temp_dirs() -> Generator[dict[str, Path], None, None]:
    base = Path(tempfile.mkdtemp(prefix="test_worker_artifact_"))
    agent_dir = base / "agent"
    agent_dir.mkdir(parents=True)
    artifacts_dir = agent_dir / "artifacts"
    artifacts_dir.mkdir(parents=True)
    inbox_dir = artifacts_dir / "inbox"
    inbox_dir.mkdir(parents=True)
    outbox_db = str(base / "outbox.db")

    dirs = {
        "base": base,
        "agent": agent_dir,
        "artifacts": artifacts_dir,
        "inbox": inbox_dir,
        "outbox_db": outbox_db,
    }
    yield dirs
    shutil.rmtree(base, ignore_errors=True)


class MockControlServer:
    """Mock HTTP Transport mô phỏng các endpoint của Control Node."""

    def __init__(self):
        self.artifacts: dict[str, dict] = {}
        self.upload_fails = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        # GET /api/v1/artifacts/{id}/content
        if request.method == "GET" and path.startswith("/api/v1/artifacts/") and path.endswith("/content"):
            parts = path.split("/")
            art_id = parts[4]
            if art_id in self.artifacts:
                art = self.artifacts[art_id]
                content = art["content"]
                headers = {"X-Checksum-SHA256": art["checksum"]}
                return httpx.Response(200, content=content, headers=headers)
            return httpx.Response(404, text=f"Artifact {art_id} not found")

        # POST /api/v1/artifacts
        if request.method == "POST" and path == "/api/v1/artifacts":
            if self.upload_fails:
                return httpx.Response(500, text="Internal Server Error: Artifact Store disk full")

            art_id = f"ART-{len(self.artifacts) + 1:03d}"
            uploaded_name = "test_cases.json"
            # Trả về JSON của ArtifactRef
            resp_data = {
                "artifact_id": art_id,
                "name": uploaded_name,
                "type": "TEST_CASE",
                "version": "1.0",
                "uri": f"artifact://{art_id}",
                "created_by": "tester-01",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "checksum": "abcdef0123456789",
            }
            return httpx.Response(201, json=resp_data)

        # POST /api/v1/tasks/{id}/result
        if request.method == "POST" and "/api/v1/tasks/" in path and path.endswith("/result"):
            return httpx.Response(200, json={"status": "ACK"})

        return httpx.Response(404, text="Not Found")


class TestWorkerArtifactWorkflow:
    """Kiểm tra quy trình xử lý Artifact tự động trên Worker."""

    def test_download_artifact_success(self, temp_dirs: dict[str, Any]):
        mock_control = MockControlServer()
        content = b"Specification content"
        sha = hashlib.sha256(content).hexdigest()
        mock_control.artifacts["ART-SPEC-01"] = {
            "content": content,
            "checksum": sha,
            "name": "spec.md",
        }

        transport = httpx.MockTransport(mock_control.handle_request)
        http_client = httpx.Client(transport=transport)

        outbox = OutboxService(db_path=temp_dirs["outbox_db"])
        fake_agent = FakeAgent(agent_id="dev-01", role=AgentRole.DEV)
        fake_agent.artifact_dir = temp_dirs["artifacts"]

        runner = AgentRunner(
            agent_id="dev-01",
            role=AgentRole.DEV,
            agent_instance=fake_agent,
            outbox=outbox,
            secret="test_secret",
            control_url="http://mock-control",
            http_client=http_client,
        )

        input_art = ArtifactRef(
            artifact_id="ART-SPEC-01",
            name="spec.md",
            type=ArtifactType.REQUIREMENT,
            uri="artifact://ART-SPEC-01",
            checksum=sha,
            created_by="ba-01",
            created_at=datetime.now(timezone.utc),
        )

        # Thêm artifact github:// để đảm bảo không bị cố tải
        github_art = ArtifactRef(
            artifact_id="ART-GIT-01",
            name="pr-code",
            type=ArtifactType.SOURCE_CODE,
            uri="github://vietanh910/AGENT/pull/1",
            created_by="dev-01",
            created_at=datetime.now(timezone.utc),
        )

        req = DispatchRequest(
            task_id="TASK-DEV-001",
            project_id="RUN-1",
            agent_id="dev-01",
            role=AgentRole.DEV,
            action="implement_feature",
            instruction="Implement feature from spec",
            callback_url="http://mock-control/api/v1/tasks/TASK-DEV-001/result",
            input_artifacts=[input_art, github_art],
        )

        # Chạy trực tiếp qua thread runner
        runner._run_task_thread(req)

        # Kiểm tra file đã được lưu vào inbox
        downloaded_file = temp_dirs["inbox"] / "spec.md"
        assert downloaded_file.exists()
        assert downloaded_file.read_bytes() == content

        # Kết quả trong cache phải là COMPLETED
        cached = outbox.get_cached_result("TASK-DEV-001")
        assert cached is not None
        assert cached.status == TaskStatus.COMPLETED

    def test_download_artifact_checksum_mismatch_blocks_task(self, temp_dirs: dict[str, Any]):
        mock_control = MockControlServer()
        content = b"Tampered content"
        correct_sha = hashlib.sha256(content).hexdigest()
        bad_sha = "0000000000000000000000000000000000000000000000000000000000000000"

        mock_control.artifacts["ART-CORRUPTED"] = {
            "content": content,
            "checksum": correct_sha,  # Header trả về correct_sha nhưng request ghi bad_sha
            "name": "spec.md",
        }

        transport = httpx.MockTransport(mock_control.handle_request)
        http_client = httpx.Client(transport=transport)

        outbox = OutboxService(db_path=temp_dirs["outbox_db"])
        fake_agent = FakeAgent(agent_id="dev-01", role=AgentRole.DEV)
        fake_agent.artifact_dir = temp_dirs["artifacts"]

        runner = AgentRunner(
            agent_id="dev-01",
            role=AgentRole.DEV,
            agent_instance=fake_agent,
            outbox=outbox,
            secret="test_secret",
            control_url="http://mock-control",
            http_client=http_client,
        )

        bad_art = ArtifactRef(
            artifact_id="ART-CORRUPTED",
            name="corrupted.md",
            type=ArtifactType.REQUIREMENT,
            uri="artifact://ART-CORRUPTED",
            checksum=bad_sha,
            created_by="ba-01",
            created_at=datetime.now(timezone.utc),
        )

        req = DispatchRequest(
            task_id="TASK-DEV-CORRUPT",
            project_id="RUN-1",
            agent_id="dev-01",
            role=AgentRole.DEV,
            action="implement_feature",
            instruction="Implement feature",
            callback_url="http://mock-control/api/v1/tasks/TASK-DEV-CORRUPT/result",
            input_artifacts=[bad_art],
        )

        # Chạy task
        runner._run_task_thread(req)

        # Kết quả phải là BLOCKED với mã MISSING_CONTEXT
        cached = outbox.get_cached_result("TASK-DEV-CORRUPT")
        assert cached is not None
        assert cached.status == TaskStatus.BLOCKED
        assert cached.error is not None
        assert cached.error.code == ErrorCode.MISSING_CONTEXT

    def test_upload_artifacts_after_task_completion(self, temp_dirs: dict[str, Any]):
        mock_control = MockControlServer()
        transport = httpx.MockTransport(mock_control.handle_request)
        http_client = httpx.Client(transport=transport)

        outbox = OutboxService(db_path=temp_dirs["outbox_db"])
        fake_agent = FakeAgent(agent_id="tester-01", role=AgentRole.TESTER)
        fake_agent.artifact_dir = temp_dirs["artifacts"]

        # Giả lập agent tạo ra 1 artifact trên đĩa và ghi vào index.json
        test_file = temp_dirs["artifacts"] / "test_cases.json"
        test_file.write_text('{"cases": ["test_login", "test_logout"]}', encoding="utf-8")

        index_file = temp_dirs["artifacts"] / "index.json"
        index_file.write_text(
            json.dumps([{
                "artifact_id": "ART-LOCAL-01",
                "name": "test_cases.json",
                "type": "TEST_CASE",
                "uri": "test_cases.json",
                "created_by": "tester-01",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }]),
            encoding="utf-8",
        )

        runner = AgentRunner(
            agent_id="tester-01",
            role=AgentRole.TESTER,
            agent_instance=fake_agent,
            outbox=outbox,
            secret="test_secret",
            control_url="http://mock-control",
            http_client=http_client,
        )

        req = DispatchRequest(
            task_id="TASK-TEST-001",
            project_id="RUN-1",
            agent_id="tester-01",
            role=AgentRole.TESTER,
            action="create_test_cases",
            instruction="Create test cases",
            callback_url="http://mock-control/api/v1/tasks/TASK-TEST-001/result",
        )

        runner._run_task_thread(req)

        # Kết quả trong outbox phải có URI đã đổi thành artifact://ART-001
        cached = outbox.get_cached_result("TASK-TEST-001")
        assert cached is not None
        assert cached.status == TaskStatus.COMPLETED
        assert len(cached.artifacts) >= 1
        uploaded_art = next((a for a in cached.artifacts if "test_cases" in a.name), None)
        assert uploaded_art is not None
        assert uploaded_art.uri.startswith("artifact://")

    def test_upload_failure_marks_task_as_failed_retryable(self, temp_dirs: dict[str, Any]):
        mock_control = MockControlServer()
        mock_control.upload_fails = True  # Giả lập server Control Node bị lỗi 500 khi upload

        transport = httpx.MockTransport(mock_control.handle_request)
        http_client = httpx.Client(transport=transport)

        outbox = OutboxService(db_path=temp_dirs["outbox_db"])
        fake_agent = FakeAgent(agent_id="tester-01", role=AgentRole.TESTER)
        fake_agent.artifact_dir = temp_dirs["artifacts"]

        # Tạo artifact để upload
        test_file = temp_dirs["artifacts"] / "test_report.json"
        test_file.write_text('{"verdict": "PASS"}', encoding="utf-8")

        index_file = temp_dirs["artifacts"] / "index.json"
        index_file.write_text(
            json.dumps([{
                "artifact_id": "ART-LOCAL-02",
                "name": "test_report.json",
                "type": "TEST_REPORT",
                "uri": "test_report.json",
                "created_by": "tester-01",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }]),
            encoding="utf-8",
        )

        runner = AgentRunner(
            agent_id="tester-01",
            role=AgentRole.TESTER,
            agent_instance=fake_agent,
            outbox=outbox,
            secret="test_secret",
            control_url="http://mock-control",
            http_client=http_client,
        )

        req = DispatchRequest(
            task_id="TASK-UPLOAD-FAIL",
            project_id="RUN-1",
            agent_id="tester-01",
            role=AgentRole.TESTER,
            action="run_test_suite",
            instruction="Run test suite",
            callback_url="http://mock-control/api/v1/tasks/TASK-UPLOAD-FAIL/result",
        )

        runner._run_task_thread(req)

        # Upload lỗi -> Kết quả KHÔNG ĐƯỢC COI LÀ COMPLETED! Phải là FAILED có retryable=True
        cached = outbox.get_cached_result("TASK-UPLOAD-FAIL")
        assert cached is not None
        assert cached.status == TaskStatus.FAILED
        assert cached.error is not None
        assert cached.error.code == ErrorCode.ARTIFACT_ERROR
        assert cached.error.retryable is True
