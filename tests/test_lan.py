import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(ROOT_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT_DIR / "scripts"))

from agent_contract import CONTRACT_VERSION
from agent_contract.enums import AgentRole, ArtifactType, ErrorCode, TaskStatus
from agent_contract.models import ArtifactRef, Constraints, TaskRequest, TaskResult
import lan_check
import lan_client


def get_free_port() -> int:
    """Lấy một cổng ngẫu nhiên khả dụng trên máy cục bộ."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# =============================================================================
# 1. KIỂM THỬ XÁC THỰC VÀ KHỞI ĐỘNG (Authentication & Startup Refusal)
# =============================================================================
class TestAgentAPIAuthAndStartup:
    @pytest.fixture(autouse=True)
    def setup_app(self, monkeypatch):
        for key in list(sys.modules.keys()):
            if key in ["agent", "config", "llm", "prompt", "schemas", "api", "tools"] or key.startswith("tools."):
                sys.modules.pop(key, None)
        pm_dir = ROOT_DIR / "agents" / "pm"
        if str(pm_dir) not in sys.path:
            sys.path.insert(0, str(pm_dir))
        monkeypatch.setenv("AGENT_API_KEY", "test-secret-key-999")
        monkeypatch.setenv("HOST", "127.0.0.1")

        import api
        self.api = api
        self.app = api.app
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.valid_headers = {"Authorization": "Bearer test-secret-key-999"}

    def test_health_endpoint_public_without_token(self):
        """GET /health không yêu cầu Bearer token, trả về 200 với các trường bắt buộc."""
        res = self.client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] in ["ok", "healthy"]
        assert data["agent_id"] == "pm-01"
        assert data["role"] == "PM"
        assert data["contract_version"] == CONTRACT_VERSION
        assert "agent_version" in data
        assert "timestamp" in data

    def test_unauthenticated_requests_return_401(self):
        """Các endpoint khác ngoài /health nếu không có token đều trả HTTP 401."""
        endpoints = [
            ("GET", "/agent"),
            ("GET", "/state"),
            ("POST", "/tasks"),
            ("GET", "/tasks/test-task-1"),
            ("GET", "/artifacts"),
            ("GET", "/artifacts/some-id/content"),
            ("POST", "/artifacts/inbox"),
        ]
        for method, path in endpoints:
            if method == "GET":
                res = self.client.get(path)
            else:
                res = self.client.post(path)
            assert res.status_code == 401, f"{method} {path} phải trả 401 khi không có token, nhận {res.status_code}"

    def test_invalid_bearer_token_returns_401(self):
        """Sử dụng Bearer token sai phải trả HTTP 401."""
        bad_headers = {"Authorization": "Bearer wrong-token-xyz"}
        res = self.client.get("/agent", headers=bad_headers)
        assert res.status_code == 401
        assert "không hợp lệ" in res.json().get("detail", "")

    def test_valid_bearer_token_returns_200(self):
        """Sử dụng Bearer token đúng trả về dữ liệu thành công HTTP 200."""
        res = self.client.get("/agent", headers=self.valid_headers)
        assert res.status_code == 200
        data = res.json()
        assert data["agent_id"] == "pm-01"
        assert data["role"] == "PM"

    def test_startup_refusal_when_remote_host_without_api_key(self, tmp_path):
        """Nếu HOST khác 127.0.0.1 (mở LAN/WAN) mà thiếu AGENT_API_KEY thì từ chối khởi động."""
        from config import PMConfig
        cfg_remote = PMConfig(
            base_dir=tmp_path,
            port=8080,
            host="0.0.0.0",
            agent_api_key="",
        )
        with pytest.raises(RuntimeError) as exc_info:
            cfg_remote.verify_startup(check_port=False)
        assert "Bảo mật: HOST đang cấu hình là '0.0.0.0'" in str(exc_info.value)
        assert "Bắt buộc phải cấu hình AGENT_API_KEY" in str(exc_info.value)

    def test_startup_allowed_when_localhost_without_api_key(self, tmp_path):
        """Nếu HOST là 127.0.0.1 (nội bộ), không cấu hình AGENT_API_KEY vẫn cho phép khởi động."""
        from config import PMConfig
        cfg_local = PMConfig(
            base_dir=tmp_path,
            port=8080,
            host="127.0.0.1",
            agent_api_key="",
        )
        # Không ném RuntimeError
        cfg_local.verify_startup(check_port=False)


# =============================================================================
# 2. KIỂM THỬ POST /tasks (Idempotency, Concurrency Lock, Timeout)
# =============================================================================
class TestTaskProcessingBehavior:
    @pytest.fixture(autouse=True)
    def setup_app(self, monkeypatch, tmp_path):
        for key in list(sys.modules.keys()):
            if key in ["agent", "config", "llm", "prompt", "schemas", "api", "tools"] or key.startswith("tools."):
                sys.modules.pop(key, None)
        pm_dir = ROOT_DIR / "agents" / "pm"
        if str(pm_dir) not in sys.path:
            sys.path.insert(0, str(pm_dir))
        monkeypatch.setenv("AGENT_API_KEY", "test-secret-key-999")
        monkeypatch.setenv("HOST", "127.0.0.1")
        monkeypatch.setenv("LLM_API_KEY", "")

        import api
        self.api = api
        self.api.llm_client.mock = True
        self.api.agent_instance.llm.mock = True
        self.app = api.app
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.headers = {"Authorization": "Bearer test-secret-key-999"}

    def test_post_tasks_idempotency(self, tmp_path):
        """Gọi hai lần cùng một task_id thì lần hai trả lại kết quả cũ từ cache, không chạy lại."""
        saved_file = Path(self.api.config.data_dir) / "results" / "TASK-IDEMP-LAN-001.json"
        if saved_file.exists():
            saved_file.unlink()

        task_req = {
            "contract_version": "1.0.0",
            "task_id": "TASK-IDEMP-LAN-001",
            "project_id": "PROJ-LAN-001",
            "agent_id": "pm-01",
            "role": "PM",
            "action": "create_plan",
            "instruction": "Lập kế hoạch sprint 1 cho dự án",
            "context": {"project_name": "E-Commerce", "duration_weeks": 2},
        }

        try:
            # Lần 1
            res1 = self.client.post("/tasks", json=task_req, headers=self.headers)
            assert res1.status_code == 200
            result1 = res1.json()
            assert result1["status"] == "COMPLETED"
            assert result1["task_id"] == "TASK-IDEMP-LAN-001"

            # Kiểm tra file đã lưu trong data/results/
            assert saved_file.exists()

            # Lần 2: gọi lại với cùng task_id
            res2 = self.client.post("/tasks", json=task_req, headers=self.headers)
            assert res2.status_code == 200
            result2 = res2.json()
            assert result2 == result1

            # GET /tasks/{task_id} cũng trả về kết quả đó
            res_get = self.client.get("/tasks/TASK-IDEMP-LAN-001", headers=self.headers)
            assert res_get.status_code == 200
            assert res_get.json() == result1
        finally:
            if saved_file.exists():
                saved_file.unlink()

    def test_post_tasks_concurrency_conflict_409(self):
        """Khi agent đang bận xử lý task khác (task_lock đang bị giữ), task thứ hai trả về HTTP 409 Conflict."""
        task_req = {
            "contract_version": "1.0.0",
            "task_id": "TASK-CONCURRENCY-002",
            "project_id": "PROJ-LAN-001",
            "agent_id": "pm-01",
            "role": "PM",
            "action": "create_plan",
            "instruction": "Lập kế hoạch sprint khi bận",
        }

        # Giả lập lock đang bị chiếm bởi task khác
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.api.task_lock.acquire())
        try:
            res = self.client.post("/tasks", json=task_req, headers=self.headers)
            assert res.status_code == 409
            assert "bận xử lý" in res.json().get("detail", "")
        finally:
            self.api.task_lock.release()
            loop.close()

    def test_post_tasks_timeout_returns_failed_timeout(self, monkeypatch):
        """Khi thời gian thực thi vượt quá timeout_seconds, trả về TaskResult FAILED/TIMEOUT và retryable=True."""
        task_req = {
            "contract_version": "1.0.0",
            "task_id": "TASK-TIMEOUT-003",
            "project_id": "PROJ-LAN-001",
            "agent_id": "pm-01",
            "role": "PM",
            "action": "create_plan",
            "instruction": "Task chạy chậm để kiểm tra timeout",
            "constraints": {"timeout_seconds": 1},
        }

        # Mock agent_instance.handle để sleep 2 giây
        def mock_slow_handle(req):
            time.sleep(2)
            return TaskResult(
                contract_version="1.0.0",
                task_id=req.task_id,
                agent_id=req.agent_id,
                status=TaskStatus.COMPLETED,
                summary="Done slowly",
            )

        monkeypatch.setattr(self.api.agent_instance, "handle", mock_slow_handle)

        res = self.client.post("/tasks", json=task_req, headers=self.headers)
        assert res.status_code == 200
        result = res.json()
        assert result["status"] == "FAILED"
        assert result["error"] is not None
        assert result["error"]["code"] == "TIMEOUT"
        assert result["error"]["retryable"] is True


# =============================================================================
# 3. KIỂM THỬ ARTIFACT QUA MẠNG (Download, Upload Inbox, Security)
# =============================================================================
class TestArtifactNetworkSecurity:
    @pytest.fixture(autouse=True)
    def setup_app(self, monkeypatch, tmp_path):
        for key in list(sys.modules.keys()):
            if key in ["agent", "config", "llm", "prompt", "schemas", "api", "tools"] or key.startswith("tools."):
                sys.modules.pop(key, None)
        pm_dir = ROOT_DIR / "agents" / "pm"
        if str(pm_dir) not in sys.path:
            sys.path.insert(0, str(pm_dir))
        monkeypatch.setenv("AGENT_API_KEY", "test-secret-key-999")
        monkeypatch.setenv("HOST", "127.0.0.1")
        monkeypatch.setenv("MAX_UPLOAD_MB", "1")

        import api
        self.api = api
        self.client = TestClient(api.app, raise_server_exceptions=False)
        self.headers = {"Authorization": "Bearer test-secret-key-999"}

    def test_artifact_download_checksum_header_and_traversal_protection(self):
        """GET /artifacts/{id}/content trả về file cùng header X-Checksum-SHA256, chặn path traversal."""
        art_dir = Path(self.api.config.artifact_dir)
        art_dir.mkdir(parents=True, exist_ok=True)
        content = b"# Document Content for Download Test"
        sha256 = hashlib.sha256(content).hexdigest()

        test_file = art_dir / "test_download.md"
        test_file.write_bytes(content)

        index_file = art_dir / "index.json"
        index_data = [{
            "artifact_id": "ART-DL-001",
            "name": "test_download.md",
            "type": "DOCUMENTATION",
            "version": "1.0",
            "uri": "artifacts/test_download.md",
            "created_by": "pm-01",
            "created_at": "2026-11-03T00:00:00Z",
            "checksum": sha256,
        }]
        index_file.write_text(json.dumps(index_data), encoding="utf-8")

        # 1. Tải thành công
        res = self.client.get("/artifacts/ART-DL-001/content", headers=self.headers)
        assert res.status_code == 200
        assert res.content == content
        assert res.headers.get("X-Checksum-SHA256") == sha256

        # 2. Không tìm thấy ID
        res_404 = self.client.get("/artifacts/ART-NOT-EXIST/content", headers=self.headers)
        assert res_404.status_code == 404

        # 3. Path traversal attack
        res_evil = self.client.get("/artifacts/../../etc/passwd/content", headers=self.headers)
        assert res_evil.status_code in [404, 422, 400]

    def test_artifact_inbox_upload_exceeds_max_upload_mb(self):
        """Tải lên vượt quá MAX_UPLOAD_MB (1MB) bị từ chối với HTTP 413."""
        big_content = b"X" * (1024 * 1024 + 1024)  # > 1MB
        sha = hashlib.sha256(big_content).hexdigest()
        metadata = {
            "artifact_id": "ART-BIG-001",
            "name": "large_file.bin",
            "type": "OTHER",
            "version": "1.0",
            "uri": "artifacts/inbox/devops-01/large_file.bin",
            "created_by": "devops-01",
            "created_at": "2026-11-03T00:00:00Z",
            "checksum": sha,
        }
        files = {"file": ("large_file.bin", big_content, "application/octet-stream")}
        data = {"metadata": json.dumps(metadata)}

        res = self.client.post("/artifacts/inbox", data=data, files=files, headers=self.headers)
        assert res.status_code == 413
        assert "vượt quá giới hạn cho phép" in res.json().get("detail", "")

    def test_artifact_inbox_upload_checksum_mismatch(self):
        """Tải lên có SHA-256 thực tế lệch với checksum trong metadata bị từ chối 422 ARTIFACT_ERROR."""
        content = b"Test Checksum Mismatch Content"
        metadata = {
            "artifact_id": "ART-MISMATCH-001",
            "name": "doc.md",
            "type": "DOCUMENTATION",
            "version": "1.0",
            "uri": "artifacts/inbox/ba-01/doc.md",
            "created_by": "ba-01",
            "created_at": "2026-11-03T00:00:00Z",
            "checksum": "1111222233334444555566667777888899990000aaaaabbbbbcccccdddddeeeee",
        }
        files = {"file": ("doc.md", content, "text/markdown")}
        data = {"metadata": json.dumps(metadata)}

        res = self.client.post("/artifacts/inbox", data=data, files=files, headers=self.headers)
        assert res.status_code == 422
        assert "Checksum sha256 không khớp" in res.json().get("detail", "")

    def test_artifact_inbox_filename_path_traversal_sanitized(self):
        """Tên file chứa đường dẫn tương đối ../ hoặc tuyệt đối /etc/ được làm sạch khi lưu vào inbox."""
        content = b"Safe Content from malicious filename"
        sha = hashlib.sha256(content).hexdigest()
        metadata = {
            "artifact_id": "ART-SAFE-001",
            "name": "../../evil_script.sh",
            "type": "SOURCE_CODE",
            "version": "1.0",
            "uri": "artifacts/inbox/tester-01/evil_script.sh",
            "created_by": "tester-01",
            "created_at": "2026-11-03T00:00:00Z",
            "checksum": sha,
        }
        files = {"file": ("../../evil_script.sh", content, "text/plain")}
        data = {"metadata": json.dumps(metadata)}

        res = self.client.post("/artifacts/inbox", data=data, files=files, headers=self.headers)
        assert res.status_code == 200

        # File phải được lưu vào artifacts/inbox/tester-01/evil_script.sh
        inbox_dest = Path(self.api.config.artifact_dir) / "inbox" / "tester-01" / "evil_script.sh"
        assert inbox_dest.exists()
        assert inbox_dest.read_bytes() == content

    def test_task_missing_input_artifacts_blocked(self):
        """TaskRequest có input_artifacts nhưng chưa có trong inbox/ trả về BLOCKED và MISSING_CONTEXT."""
        task_req = {
            "contract_version": "1.0.0",
            "task_id": "TASK-NEED-ART-001",
            "project_id": "PROJ-LAN-001",
            "agent_id": "pm-01",
            "role": "PM",
            "action": "create_plan",
            "instruction": "Lập kế hoạch dựa trên requirement",
            "input_artifacts": [
                {
                    "artifact_id": "ART-MISSING-SRS-001",
                    "name": "srs.md",
                    "type": "REQUIREMENT",
                    "version": "1.0",
                    "uri": "artifacts/inbox/ba-01/srs.md",
                    "created_by": "ba-01",
                    "created_at": "2026-11-03T00:00:00Z",
                    "checksum": "abcdef1234567890",
                }
            ],
        }

        res = self.client.post("/tasks", json=task_req, headers=self.headers)
        assert res.status_code == 200
        result = res.json()
        assert result["status"] == "BLOCKED"
        assert result["error"] is not None
        assert result["error"]["code"] == "MISSING_CONTEXT"
        assert "ART-MISSING-SRS-001" in result["error"]["details"].get("missing_artifacts", [])


# =============================================================================
# 4. KIỂM THỬ TÍCH HỢP 2 AGENT QUA MẠNG LAN DÙNG LAN_CLIENT & LAN_CHECK
# =============================================================================
@pytest.fixture(scope="module")
def run_two_agents():
    """Khởi động 2 agent PM và DevOps trên 2 port ngẫu nhiên trong background."""
    port_pm = get_free_port()
    port_devops = get_free_port()
    while port_devops == port_pm:
        port_devops = get_free_port()

    token_pm = "token-pm-secret-lan-123"
    token_devops = "token-devops-secret-lan-456"

    env_pm = os.environ.copy()
    env_pm["HOST"] = "127.0.0.1"
    env_pm["PORT"] = str(port_pm)
    env_pm["AGENT_API_KEY"] = token_pm
    env_pm["LLM_API_KEY"] = ""
    env_pm["DRY_RUN"] = "true"

    env_devops = os.environ.copy()
    env_devops["HOST"] = "127.0.0.1"
    env_devops["PORT"] = str(port_devops)
    env_devops["AGENT_API_KEY"] = token_devops
    env_devops["LLM_API_KEY"] = ""
    env_devops["DRY_RUN"] = "true"

    pm_dir = ROOT_DIR / "agents" / "pm"
    devops_dir = ROOT_DIR / "agents" / "devops"

    proc_pm = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--port", str(port_pm), "--host", "127.0.0.1"],
        cwd=str(pm_dir),
        env=env_pm,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    proc_devops = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "api:app", "--port", str(port_devops), "--host", "127.0.0.1"],
        cwd=str(devops_dir),
        env=env_devops,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Chờ 2 agent khởi động
    start_time = time.time()
    ready_pm = False
    ready_devops = False
    while time.time() - start_time < 15:
        try:
            if not ready_pm and httpx.get(f"http://127.0.0.1:{port_pm}/health", timeout=1.0).status_code == 200:
                ready_pm = True
        except Exception:
            pass
        try:
            if not ready_devops and httpx.get(f"http://127.0.0.1:{port_devops}/health", timeout=1.0).status_code == 200:
                ready_devops = True
        except Exception:
            pass
        if ready_pm and ready_devops:
            break
        time.sleep(0.5)

    assert ready_pm and ready_devops, "Không thể khởi động 2 agent PM và DevOps trong thời gian chờ."

    yield {
        "pm": {"port": port_pm, "url": f"http://127.0.0.1:{port_pm}", "token": token_pm, "dir": pm_dir},
        "devops": {"port": port_devops, "url": f"http://127.0.0.1:{port_devops}", "token": token_devops, "dir": devops_dir},
    }

    # Dừng 2 agent
    for proc in [proc_pm, proc_devops]:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            proc.kill()


class TestTwoAgentsLANIntegration:
    def test_lan_check_report(self, run_two_agents, tmp_path, monkeypatch):
        """scripts/lan_check.py báo PASS cho cả 2 agent và tạo báo cáo Markdown."""
        agents = run_two_agents
        endpoints_file = ROOT_DIR / "scripts" / "endpoints.yaml"
        yaml_content = {
            "pm-01": agents["pm"]["url"],
            "devops-01": agents["devops"]["url"],
        }
        with open(endpoints_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f)

        report_file = ROOT_DIR / "docs" / "lan-check-report.md"

        try:
            exit_code = lan_check.main()
            assert exit_code == 0
            assert report_file.exists()
            content = report_file.read_text(encoding="utf-8")
            assert "pm-01" in content
            assert "devops-01" in content
            assert "ĐỒNG NHẤT" in content
        finally:
            if endpoints_file.exists():
                endpoints_file.unlink()

    def test_lan_client_push_artifact_and_dispatch_task(self, run_two_agents, tmp_path, monkeypatch):
        """Dùng lan_client đẩy file từ máy PM sang inbox DevOps, sau đó giao task DevOps xử lý file đó."""
        agents = run_two_agents
        endpoints_file = ROOT_DIR / "scripts" / "endpoints.yaml"
        yaml_content = {
            "pm-01": agents["pm"]["url"],
            "devops-01": agents["devops"]["url"],
        }
        with open(endpoints_file, "w", encoding="utf-8") as f:
            yaml.dump(yaml_content, f)

        monkeypatch.setenv("AGENT_API_KEY_PM_01", agents["pm"]["token"])
        monkeypatch.setenv("AGENT_API_KEY_DEVOPS_01", agents["devops"]["token"])

        try:
            # 1. Tạo file artifact mẫu
            doc_file = tmp_path / "architecture_spec.md"
            doc_content = b"# Architecture Specification v1.0\nDesigned by PM"
            doc_file.write_bytes(doc_content)
            doc_sha = hashlib.sha256(doc_content).hexdigest()

            # 2. Đẩy artifact sang inbox của DevOps bằng lan_client
            push_res = lan_client.cmd_push_artifact(
                agent_id="devops-01",
                file_path_str=str(doc_file),
                artifact_type="REQUIREMENT",
                created_by="pm-01",
            )
            assert push_res == 0

            # Xác nhận file đã có trong inbox của DevOps
            devops_inbox_file = agents["devops"]["dir"] / "artifacts" / "inbox" / "pm-01" / "architecture_spec.md"
            assert devops_inbox_file.exists()
            assert devops_inbox_file.read_bytes() == doc_content

            # 3. Tạo task JSON cho DevOps có input_artifacts trỏ tới artifact vừa đẩy
            task_data = {
                "contract_version": "1.0.0",
                "task_id": "TASK-LAN-DEVOPS-001",
                "project_id": "PROJ-LAN-001",
                "agent_id": "devops-01",
                "role": "DEVOPS",
                "action": "git_operation",
                "instruction": "Thực hiện thao tác git dựa trên đặc tả kiến trúc",
                "context": {"operation": "pull", "branch": "main"},
                "input_artifacts": [
                    {
                        "artifact_id": "ART-LAN-SPEC-001",
                        "name": "architecture_spec.md",
                        "type": "REQUIREMENT",
                        "version": "1.0",
                        "uri": "artifacts/inbox/pm-01/architecture_spec.md",
                        "created_by": "pm-01",
                        "created_at": "2026-11-03T00:00:00Z",
                        "checksum": doc_sha,
                    }
                ],
            }
            task_file = tmp_path / "devops_task.json"
            task_file.write_text(json.dumps(task_data), encoding="utf-8")

            # 4. Giao task cho DevOps bằng lan_client
            task_res = lan_client.cmd_task("devops-01", str(task_file))
            assert task_res == 0

            # 5. Kéo artifact từ PM bằng lan_client
            pm_art_dir = agents["pm"]["dir"] / "artifacts"
            pm_art_file = pm_art_dir / "sprint_plan.md"
            pm_content = b"# Sprint Plan\nPM Sprint 1"
            pm_sha = hashlib.sha256(pm_content).hexdigest()
            pm_art_file.write_bytes(pm_content)

            pm_index = pm_art_dir / "index.json"
            existing_index = []
            if pm_index.exists():
                try:
                    existing_index = json.loads(pm_index.read_text(encoding="utf-8"))
                except Exception:
                    pass
            existing_index.append({
                "artifact_id": "ART-PM-PLAN-001",
                "name": "sprint_plan.md",
                "type": "PLAN",
                "version": "1.0",
                "uri": "artifacts/sprint_plan.md",
                "created_by": "pm-01",
                "created_at": "2026-11-03T00:00:00Z",
                "checksum": pm_sha,
            })
            pm_index.write_text(json.dumps(existing_index), encoding="utf-8")

            out_download_dir = tmp_path / "pulled_artifacts"
            pull_res = lan_client.cmd_pull_artifact(
                agent_id="pm-01",
                artifact_id="ART-PM-PLAN-001",
                out_dir_str=str(out_download_dir),
            )
            assert pull_res == 0
            pulled_file = out_download_dir / "sprint_plan.md"
            assert pulled_file.exists()
            assert hashlib.sha256(pulled_file.read_bytes()).hexdigest() == pm_sha
        finally:
            if endpoints_file.exists():
                endpoints_file.unlink()
