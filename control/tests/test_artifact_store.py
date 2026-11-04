"""Bộ kiểm thử cho Artifact Store trên PC1 (control/tests/test_artifact_store.py).

Kiểm tra:
1. Tải lên artifact thành công, tính toán mã băm SHA-256.
2. Từ chối 422 khi checksum client gửi không khớp với thực tế.
3. Từ chối 413 khi kích thước vượt MAX_UPLOAD_MB.
4. Làm sạch tên file độc hại (path traversal, .., ký tự lạ, đường dẫn tuyệt đối).
5. Phiên bản bất biến (không ghi đè file cũ khi upload cùng tên/ID).
6. Tải nội dung artifact qua GET /artifacts/{id}/content kèm header X-Checksum-SHA256.
7. Liệt kê artifact của run qua GET /runs/{id}/artifacts?type=&latest=true.
8. Hàm select_input_artifacts(session, task) chọn đúng phiên bản mới nhất theo action.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import Session, sessionmaker

from agent_contract.enums import AgentRole, ArtifactType
from agent_contract.models import ArtifactRef
from control.app.api import api_v1_router
from control.app.api.dependencies import get_db, require_authenticated
from control.app.config import settings
from control.app.database.models import ArtifactModel, Base, TaskModel, WorkflowRunModel
from control.app.database.repositories import ArtifactRepository, TaskRepository, WorkflowRunRepository
from control.app.security.authentication import AdminPrincipal
from control.app.services.artifact_store import (
    ArtifactStoreError,
    ArtifactStoreService,
    sanitize_filename,
    select_input_artifacts,
)
from fastapi import FastAPI


@pytest.fixture
def tmp_artifact_dir() -> Generator[Path, None, None]:
    """Tạo thư mục tạm thời cho Artifact Store."""
    tmp = Path(tempfile.mkdtemp(prefix="test_artifact_store_"))
    yield tmp
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    """Tạo SQLite in-memory DB Session cho test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        echo=False,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def test_app(db_session: Session, tmp_artifact_dir: Path) -> FastAPI:
    """Tạo FastAPI app test với dependencies ghi đè."""
    app = FastAPI()
    app.include_router(api_v1_router)

    # Ghi đè artifact_service của api
    from control.app.api.artifacts import artifact_service as api_art_svc
    from control.app.api.runs import artifact_service as run_art_svc

    custom_svc = ArtifactStoreService(base_dir=tmp_artifact_dir)
    api_art_svc.base_dir = tmp_artifact_dir
    run_art_svc.base_dir = tmp_artifact_dir

    admin_principal = AdminPrincipal(actor_id="admin-tester")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[require_authenticated] = lambda: admin_principal

    return app


@pytest.fixture
def async_client(test_app: FastAPI) -> Generator[httpx.AsyncClient, None, None]:
    transport = httpx.ASGITransport(app=test_app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    yield client


class TestArtifactSanitizationAndLimits:
    """Kiểm tra quy tắc làm sạch tên file và giới hạn kích thước."""

    def test_sanitize_filename_removes_traversal_and_paths(self):
        assert sanitize_filename("../../etc/passwd") == "passwd"
        assert sanitize_filename("..\\..\\windows\\system32\\cmd.exe") == "cmd.exe"
        assert sanitize_filename("/var/log/syslog") == "syslog"
        assert sanitize_filename("C:\\Users\\Admin\\secret.txt") == "secret.txt"
        assert sanitize_filename("my project#1!@$.pdf") == "my_project_1___.pdf"
        assert sanitize_filename("") == "unnamed_artifact.dat"

    def test_upload_exceeding_max_upload_mb_raises_413(self, tmp_artifact_dir: Path, db_session: Session):
        service = ArtifactStoreService(base_dir=tmp_artifact_dir)
        original_mb = settings.MAX_UPLOAD_MB
        try:
            settings.MAX_UPLOAD_MB = 1  # 1 MB
            big_content = b"X" * (1024 * 1024 + 50)  # > 1MB
            with pytest.raises(ArtifactStoreError) as exc_info:
                service.save_artifact(
                    session=db_session,
                    content=big_content,
                    filename="large_file.bin",
                    run_id="RUN-1",
                    task_id="TASK-1",
                    artifact_type=ArtifactType.SOURCE_CODE,
                    created_by="dev-01",
                )
            assert exc_info.value.status_code == 413
        finally:
            settings.MAX_UPLOAD_MB = original_mb


class TestArtifactStoreAPI:
    """Kiểm tra các endpoint REST API của Artifact Store."""

    @pytest.mark.asyncio
    async def test_upload_artifact_success(self, async_client: httpx.AsyncClient, db_session: Session):
        content = b"# Architecture Design Document\n\nComponents: Gateway, Orchestrator."
        actual_sha = hashlib.sha256(content).hexdigest()

        meta = {
            "name": "architecture.md",
            "type": "ARCHITECTURE",
            "run_id": "RUN-TEST-01",
            "task_id": "T1-SA",
            "version": "1.0",
            "checksum": actual_sha,
            "created_by": "sa-01",
        }

        files = {"file": ("architecture.md", content, "text/markdown")}
        data = {"metadata": json.dumps(meta)}

        resp = await async_client.post("/api/v1/artifacts", files=files, data=data)
        assert resp.status_code == 201

        res_json = resp.json()
        assert res_json["name"] == "architecture.md"
        assert res_json["type"] == "ARCHITECTURE"
        assert res_json["checksum"] == actual_sha
        assert res_json["uri"].startswith("artifact://")

        art_id = res_json["artifact_id"]

        # Kiểm tra tải lại nội dung
        get_resp = await async_client.get(f"/api/v1/artifacts/{art_id}/content")
        assert get_resp.status_code == 200
        assert get_resp.content == content
        assert get_resp.headers["X-Checksum-SHA256"] == actual_sha

    @pytest.mark.asyncio
    async def test_upload_artifact_checksum_mismatch_returns_422(self, async_client: httpx.AsyncClient):
        content = b"Real content"
        wrong_sha = "0000000000000000000000000000000000000000000000000000000000000000"

        meta = {
            "name": "test.txt",
            "type": "OTHER",
            "run_id": "RUN-1",
            "checksum": wrong_sha,
        }

        files = {"file": ("test.txt", content, "text/plain")}
        data = {"metadata": json.dumps(meta)}

        resp = await async_client.post("/api/v1/artifacts", files=files, data=data)
        assert resp.status_code == 422
        assert "không khớp" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_immutable_artifact_rejects_duplicate(self, async_client: httpx.AsyncClient):
        content = b"Version 1"
        fixed_id = "ART-IMMUTABLE-01"

        meta = {
            "artifact_id": fixed_id,
            "name": "doc.md",
            "type": "DOCUMENTATION",
            "run_id": "RUN-1",
        }

        files = {"file": ("doc.md", content, "text/markdown")}
        data = {"metadata": json.dumps(meta)}

        # Lần 1: Thành công
        resp1 = await async_client.post("/api/v1/artifacts", files=files, data=data)
        assert resp1.status_code == 201, f"resp1 failed: {resp1.text}"

        # Lần 2: Cố tình upload trùng ID -> Bị từ chối 409 Conflict
        files2 = {"file": ("doc.md", b"Version 2 attempt overwrite", "text/markdown")}
        resp2 = await async_client.post("/api/v1/artifacts", files=files2, data=data)
        assert resp2.status_code == 409


class TestRunArtifactListingAndSelection:
    """Kiểm tra truy vấn artifact theo run và hàm select_input_artifacts."""

    @pytest.mark.asyncio
    async def test_list_run_artifacts_latest_filter(
        self,
        async_client: httpx.AsyncClient,
        db_session: Session,
        tmp_artifact_dir: Path,
    ):
        service = ArtifactStoreService(base_dir=tmp_artifact_dir)
        run_id = "RUN-SELECT-01"

        # Upload bản 1 của ARCHITECTURE
        service.save_artifact(
            session=db_session,
            content=b"Architecture v1",
            filename="arch_v1.md",
            run_id=run_id,
            task_id="T1",
            artifact_type=ArtifactType.ARCHITECTURE,
            created_by="sa-01",
            version="1.0",
        )
        db_session.commit()

        # Upload bản 2 của ARCHITECTURE
        art_v2 = service.save_artifact(
            session=db_session,
            content=b"Architecture v2 (refined)",
            filename="arch_v2.md",
            run_id=run_id,
            task_id="T1-fix",
            artifact_type=ArtifactType.ARCHITECTURE,
            created_by="sa-01",
            version="2.0",
        )
        db_session.commit()

        # Upload DATABASE_SCHEMA
        service.save_artifact(
            session=db_session,
            content=b"Schema v1",
            filename="schema.sql",
            run_id=run_id,
            task_id="T2",
            artifact_type=ArtifactType.DATABASE_SCHEMA,
            created_by="dba-01",
            version="1.0",
        )
        db_session.commit()

        # 1. Truy vấn latest=true cho run_id
        resp_latest = await async_client.get(f"/api/v1/runs/{run_id}/artifacts?latest=true")
        assert resp_latest.status_code == 200
        items = resp_latest.json()
        assert len(items) == 2  # 1 ARCHITECTURE và 1 DATABASE_SCHEMA

        arch_item = next(i for i in items if i["type"] == "ARCHITECTURE")
        assert arch_item["artifact_id"] == art_v2.artifact_id
        assert arch_item["version"] == "2.0"

        # 2. Truy vấn latest=false -> Trả đủ 3 artifact
        resp_all = await async_client.get(f"/api/v1/runs/{run_id}/artifacts?latest=false")
        assert resp_all.status_code == 200
        assert len(resp_all.json()) == 3

    def test_select_input_artifacts_for_task(self, db_session: Session, tmp_artifact_dir: Path):
        service = ArtifactStoreService(base_dir=tmp_artifact_dir)
        run_id = "RUN-SELECT-02"

        # Tạo sẵn artifact ARCHITECTURE và DATABASE_SCHEMA trong run
        art_arch = service.save_artifact(
            session=db_session,
            content=b"Arch Spec",
            filename="arch.md",
            run_id=run_id,
            task_id="T1",
            artifact_type=ArtifactType.ARCHITECTURE,
            created_by="sa-01",
        )
        art_dba = service.save_artifact(
            session=db_session,
            content=b"DB Spec",
            filename="db.sql",
            run_id=run_id,
            task_id="T2",
            artifact_type=ArtifactType.DATABASE_SCHEMA,
            created_by="dba-01",
        )
        db_session.commit()

        # Task TESTER: action create_test_cases cần ARCHITECTURE và DATABASE_SCHEMA
        tester_task = TaskModel(
            task_id="T3-TESTER",
            run_id=run_id,
            agent_id="tester-01",
            role="TESTER",
            action="create_test_cases",
            instruction="Create test cases",
            context={},
            dispatch_state="QUEUED",
        )
        db_session.add(tester_task)
        db_session.commit()

        inputs = select_input_artifacts(db_session, tester_task)
        assert len(inputs) == 2
        input_types = {inp.type for inp in inputs}
        assert ArtifactType.ARCHITECTURE in input_types
        assert ArtifactType.DATABASE_SCHEMA in input_types

        arch_input = next(inp for inp in inputs if inp.type == ArtifactType.ARCHITECTURE)
        assert arch_input.artifact_id == art_arch.artifact_id
        assert arch_input.uri == f"artifact://{art_arch.artifact_id}"
