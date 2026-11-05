"""Read-only realtime projections, artifact provenance, authenticated delivery and stream recovery."""

from datetime import timedelta
import json
from pathlib import Path

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from control.app.api import api_v1_router
from control.app.api import dashboard as dashboard_api
from control.app.api.dependencies import get_current_principal, get_db
from control.app.config import settings
from control.app.dashboard_ui import router as ui_router
from control.app.database.models import (
    AgentEventModel, AgentModel, ArtifactModel, AuditLogModel, Base, ProjectModel, TaskDependencyModel, TaskModel, WorkerModel,
    WorkflowRunModel, utc_now,
)
from control.app.services.artifact_store import ArtifactStoreService
from control.app.services.dashboard import dashboard_snapshot


@pytest.fixture
def dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "dashboard-local-test-token")
    monkeypatch.setattr("control.app.services.artifact_store.DEFAULT_ARTIFACT_STORE_DIR", tmp_path / "artifacts")
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    now = utc_now()
    store = ArtifactStoreService(tmp_path / "artifacts")
    with factory() as session:
        session.add_all([
            WorkflowRunModel(run_id="RUN-DASH", project_id="LIB", status="RUNNING", brief="Quản lý thư viện",
                plan={"project_name": "Thư viện số"}),
            WorkflowRunModel(run_id="RUN-OTHER", project_id="OTHER", status="RUNNING", brief="Another project",
                plan={"project_name": "Other"}, created_at=now - timedelta(days=1)),
            WorkerModel(worker_id="worker-1", machine_id="PC1", base_url="http://pc1", status="OFFLINE",
                contract_version="1.4.0", worker_version="1.0.0", last_heartbeat_at=now - timedelta(minutes=2)),
            WorkerModel(worker_id="worker-3", machine_id="PC3", base_url="http://pc3", status="ONLINE",
                contract_version="1.4.0", worker_version="1.0.0", last_heartbeat_at=now),
            WorkerModel(worker_id="worker-4", machine_id="PC4", base_url="http://pc4", status="ONLINE",
                contract_version="1.4.0", worker_version="1.0.0", last_heartbeat_at=now - timedelta(minutes=2)),
            AgentModel(agent_id="ba-01", role="BA", machine="PC3", runner="Worker Host", status="IDLE", capabilities=[]),
            AgentModel(agent_id="dev-01", role="DEV", machine="PC4", runner="Worker Host", status="RUNNING", current_task="DEV-1", capabilities=[]),
            AgentModel(agent_id="pm-01", role="PM", machine="PC1", runner="Worker Host", status="IDLE", capabilities=[]),
        ])
        session.flush()
        session.add_all([
            TaskModel(task_id="BA-1", run_id="RUN-DASH", agent_id="ba-01", role="BA", action="write_requirements",
                instruction="Viết SRS module sách", dispatch_state="COMPLETED", context={"title": "Đặc tả quản lý sách", "module_id": "books"}),
            TaskModel(task_id="DEV-1", run_id="RUN-DASH", agent_id="dev-01", role="DEV", action="implement_feature",
                instruction="Thực hiện tìm kiếm sách", dispatch_state="RUNNING", attempts=1,
                context={"title": "Tìm kiếm sách", "module_id": "books", "input_artifact_sources": {"REQUIREMENT": ["BA-1"]}}),
            TaskModel(task_id="TEST-1", run_id="RUN-DASH", agent_id="tester-01", role="TESTER", action="run_test_suite",
                instruction="Test chức năng tìm kiếm", dispatch_state="QUEUED", context={"title": "Kiểm thử tìm kiếm", "module_id": "books"}),
            TaskModel(task_id="PM-LOCAL", run_id="RUN-OTHER", agent_id="pm-01", role="PM", action="recover_task",
                instruction="Khôi phục SRS", dispatch_state="RUNNING", context={"title": "PM đang tạo SRS thay BA", "pm_takeover": True}),
        ])
        session.flush()
        session.add_all([TaskDependencyModel(task_id="DEV-1", depends_on_task_id="BA-1"),
                         TaskDependencyModel(task_id="TEST-1", depends_on_task_id="DEV-1")])
        artifact = store.save_artifact(session, "# SRS\nQuản lý và tìm kiếm sách.".encode(), "srs-books.md",
            "RUN-DASH", "BA-1", "REQUIREMENT", "ba-01", metadata={"source_machine": "PC3", "source_worker_id": "worker-3"})
        artifact_id = artifact.artifact_id
        session.add(AuditLogModel(action="PROJECT_AUTO_RECOVERY", actor="pm-01", target="DEV-1", run_id="RUN-DASH",
            result="RETRY_WAIT", details={"private_token": "must-never-appear"}))
        session.add(AgentEventModel(event_id="EVENT-1", event_type="TASK_STARTED", run_id="RUN-DASH", task_id="DEV-1", payload={}))
        session.commit()
    app = FastAPI()
    app.include_router(api_v1_router)
    app.include_router(ui_router)
    from control.app.main import security_exception_handler
    from control.app.security.permissions import SecurityException
    app.add_exception_handler(SecurityException, security_exception_handler)
    def db():
        with factory() as session:
            yield session
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[dashboard_api.dashboard_session_factory] = lambda: factory
    yield {"factory": factory, "store": store, "app": app, "artifact_id": artifact_id,
           "headers": {"Authorization": "Bearer dashboard-local-test-token"}}
    engine.dispose()


def test_snapshot_uses_actual_tasks_dependencies_heartbeat_and_absolute_file_paths(dashboard):
    with dashboard["factory"]() as session:
        data = dashboard_snapshot(session, "RUN-DASH")
    agents = {a["agent_id"]: a for a in data["agents"]}
    assert len(agents) == 10
    assert agents["dev-01"]["task"]["title"] == "Tìm kiếm sách"
    assert agents["dev-01"]["connection"] == "OFFLINE"  # DB says online, but heartbeat has expired.
    assert next(task for task in data["tasks"] if task["task_id"] == "DEV-1")["worker_connection"] == "OFFLINE"
    assert agents["pm-01"]["connection"] == "LOCAL" and agents["pm-01"]["status"] == "RUNNING"
    assert agents["pm-01"]["execution_host"] == data["host"]["name"]
    assert agents["sa-01"]["connection"] == "UNREGISTERED"
    assert data["selected_run"]["progress"] == 33
    assert {tuple(sorted(e.items())) for e in data["edges"]} == {
        tuple(sorted({"from": "BA-1", "to": "DEV-1"}.items())), tuple(sorted({"from": "DEV-1", "to": "TEST-1"}.items()))}
    file = data["artifacts"][0]
    assert file["created_by"] == "ba-01" and file["source_machine"] == "PC3"
    assert file["task_title"] == "Đặc tả quản lý sách"
    assert Path(file["local_path"]).is_absolute() and Path(file["local_path"]).is_file()
    assert file["available"] and file["storage_status"] == "STORED"
    assert "must-never-appear" not in json.dumps(data)
    assert {event["type"] for event in data["activity"]} >= {"ARTIFACT_RECEIVED", "TASK_STARTED", "PROJECT_AUTO_RECOVERY"}


def test_new_file_and_lost_file_change_the_realtime_revision(dashboard):
    with dashboard["factory"]() as session:
        first = dashboard_snapshot(session)
        assert dashboard_snapshot(session)["revision"] == first["revision"]
        artifact = dashboard["store"].save_artifact(session, b"print('hello')", "source.py", "RUN-DASH", "DEV-1", "SOURCE_CODE", "dev-01")
        session.commit()
        second = dashboard_snapshot(session)
        assert first["revision"] != second["revision"]
        assert second["summary"]["files_received"] == 2
        Path(artifact.metadata_json["file_path"]).unlink()
        third = dashboard_snapshot(session)
        missing = next(file for file in third["artifacts"] if file["artifact_id"] == artifact.artifact_id)
        assert not missing["available"] and missing["download_url"] is None
        assert missing["local_path"] and third["revision"] != second["revision"]


def test_snapshot_filters_run_without_hiding_other_agents_and_masks_errors(dashboard):
    with dashboard["factory"]() as session:
        task = session.get(TaskModel, "DEV-1")
        task.error = {"message": "Authorization: Bearer my_private_test_token_1234"}
        session.commit()
        data = dashboard_snapshot(session, "RUN-OTHER")
        assert [task["task_id"] for task in data["tasks"]] == ["PM-LOCAL"]
        assert data["artifacts"] == [] and data["recent_artifacts"]
        assert any(agent["agent_id"] == "dev-01" for agent in data["agents"])
        assert "my_private_test_token_1234" not in json.dumps(data)
        with pytest.raises(LookupError):
            dashboard_snapshot(session, "NOT-FOUND")


def test_failed_quality_is_not_counted_as_completed_or_shown_as_idle(dashboard):
    with dashboard["factory"]() as session:
        worker = session.get(WorkerModel, "worker-4")
        worker.last_heartbeat_at = utc_now()
        session.add(AgentModel(agent_id="tester-01", role="TESTER", machine="PC4", runner="Worker Host", status="IDLE", capabilities=[]))
        task = session.get(TaskModel, "TEST-1")
        task.dispatch_state = "COMPLETED"
        task.result = {"outputs": [{"verdict": "FAIL"}]}
        session.commit()
        data = dashboard_snapshot(session, "RUN-DASH")
        assert next(item for item in data["tasks"] if item["task_id"] == "TEST-1")["status"] == "BLOCKED"
        assert next(agent for agent in data["agents"] if agent["agent_id"] == "tester-01")["status"] == "BLOCKED"
        assert data["selected_run"]["task_counts"]["COMPLETED"] == 1
        task.context = {**task.context, "quality_repaired": True}
        session.commit()
        assert dashboard_snapshot(session, "RUN-DASH")["selected_run"]["task_counts"]["COMPLETED"] == 2


@pytest.mark.asyncio
async def test_public_shell_does_not_expose_data_and_protected_api_requires_admin(dashboard):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard["app"]), base_url="http://test") as client:
        page = await client.get("/dashboard")
        assert page.status_code == 200 and "Trung tâm điều phối" in page.text
        assert "dashboard-local-test-token" not in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await client.get("/dashboard/assets/dashboard.js")).status_code == 200
        assert (await client.get("/dashboard/assets/not-allowed.txt")).status_code == 404
        assert (await client.get("/api/v1/dashboard/snapshot")).status_code == 401
        assert (await client.get("/api/v1/dashboard/stream")).status_code == 401
        good = await client.get("/api/v1/dashboard/snapshot", headers=dashboard["headers"])
        assert good.status_code == 200 and good.json()["agents"]
        from control.app.security.authentication import WorkerPrincipal
        dashboard["app"].dependency_overrides[get_current_principal] = lambda: WorkerPrincipal("worker-3", "PC3", ["ba-01"])
        assert (await client.get("/api/v1/dashboard/snapshot")).status_code == 403


@pytest.mark.asyncio
async def test_dashboard_download_matches_saved_file_and_rejects_missing_or_modified_files(dashboard):
    url = f"/api/v1/dashboard/artifacts/{dashboard['artifact_id']}/content"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard["app"]), base_url="http://test") as client:
        assert (await client.get(url)).status_code == 401
        response = await client.get(url, headers=dashboard["headers"])
        assert response.status_code == 200
        with dashboard["factory"]() as session:
            artifact = session.get(ArtifactModel, dashboard["artifact_id"])
            path = Path(artifact.metadata_json["file_path"])
            assert response.content == path.read_bytes()
            assert response.headers["x-checksum-sha256"] == artifact.checksum
            path.write_bytes(b"changed content")
        assert (await client.get(url, headers=dashboard["headers"])).status_code == 409
        path.unlink()
        assert (await client.get(url, headers=dashboard["headers"])).status_code == 404


@pytest.mark.asyncio
async def test_uploaded_files_record_authenticated_sender_machine_and_agent(dashboard, monkeypatch):
    from control.app.api import artifacts as api_artifacts
    from control.app.security.authentication import WorkerPrincipal
    monkeypatch.setattr(api_artifacts, "artifact_service", dashboard["store"])
    dashboard["app"].dependency_overrides[get_current_principal] = lambda: WorkerPrincipal("worker-3", "PC3", ["ba-01"])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard["app"]), base_url="http://test") as client:
        bad = await client.post("/api/v1/artifacts", files={"file": ("spoof.md", b"wrong sender")},
            data={"run_id": "RUN-DASH", "task_id": "BA-1", "created_by": "dev-01"})
        assert bad.status_code == 403
        good = await client.post("/api/v1/artifacts", files={"file": ("handoff.md", b"Actual worker handoff")},
            data={"run_id": "RUN-DASH", "task_id": "BA-1", "type": "DOCUMENTATION", "created_by": "ba-01",
                  "metadata": json.dumps({"metadata": {"source_machine": "spoofed-machine"}})})
        assert good.status_code == 201, good.text
    with dashboard["factory"]() as session:
        file = next(a for a in dashboard_snapshot(session, "RUN-DASH")["artifacts"] if a["artifact_id"] == good.json()["artifact_id"])
        assert file["created_by"] == "ba-01" and file["source_machine"] == "PC3" and file["source_worker_id"] == "worker-3"


@pytest.mark.asyncio
async def test_stream_transport_sends_sse_without_putting_token_in_url(dashboard, monkeypatch):
    async def one_snapshot(request, factory, run_id, **kwargs):
        with factory() as session:
            data = dashboard_snapshot(session, run_id, **kwargs)
        yield f"event: snapshot\ndata: {json.dumps(data)}\n\n"
    monkeypatch.setattr(dashboard_api, "snapshot_stream", one_snapshot)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard["app"]), base_url="http://test") as client:
        response = await client.get("/api/v1/dashboard/stream?run_id=RUN-DASH", headers=dashboard["headers"])
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert "event: snapshot" in response.text and "srs-books.md" in response.text
        assert "dashboard-local-test-token" not in str(response.request.url)


@pytest.mark.asyncio
async def test_stream_emits_changed_state_heartbeat_and_recovers_from_database_outage(dashboard, monkeypatch):
    original = dashboard_api.dashboard_snapshot
    reads = 0
    class Request:
        async def is_disconnected(self):
            return reads >= 4
    def flaky(session, run_id, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 3:
            raise RuntimeError("private database password must not leak")
        return original(session, run_id, **kwargs)
    monkeypatch.setattr(dashboard_api, "dashboard_snapshot", flaky)
    frames = [frame async for frame in dashboard_api.snapshot_stream(Request(), dashboard["factory"], "RUN-DASH", interval=0)]
    assert "event: snapshot" in frames[0]
    assert frames[1].startswith(": heartbeat")
    assert "event: unavailable" in frames[2] and "password" not in frames[2]
    assert "event: snapshot" in frames[3]  # Same revision must be sent again after recovery.


def test_project_picker_groups_runs_and_uses_original_business_name(dashboard):
    with dashboard["factory"]() as session:
        root = session.get(WorkflowRunModel, "RUN-DASH")
        root.project_id = "PRJ-RESTAURANT"
        root.plan = {"project_name": "PRJ-RESTAURANT"}
        root.brief = "Tôi muốn xây dựng một hệ thống quản lý nhà hàng, bao gồm quản lý bàn và thực đơn."
        session.add(WorkflowRunModel(run_id="RUN-RESTAURANT-CHANGE", project_id=root.project_id, status="PLANNING",
            brief="Thêm chức năng đặt bàn", plan={"project_name": root.project_id}, created_at=utc_now() + timedelta(minutes=1)))
        session.commit()
        data = dashboard_snapshot(session, project_id=root.project_id, view="project")
        project = next(p for p in data["projects"] if p["project_id"] == root.project_id)
        assert project["name"] == "Quản lý nhà hàng" and project["run_count"] == 2
        assert len([p for p in data["projects"] if p["project_id"] == root.project_id]) == 1
        assert {r["project_id"] for r in data["runs"]} == {root.project_id}
        assert [r["sequence"] for r in data["runs"]] == [2, 1]
        assert data["selected_run"]["run_id"] == "RUN-RESTAURANT-CHANGE"
        old = dashboard_snapshot(session, "RUN-DASH", project_id=root.project_id, view="project")
        assert old["selected_run"]["name"] == "Quản lý nhà hàng"
        assert {t["task_id"] for t in old["tasks"]} == {"BA-1", "DEV-1", "TEST-1"}
        session.add(ProjectModel(project_id=root.project_id, name="Nhà hàng An Nhiên", root_run_id=root.run_id))
        session.commit()
        assert dashboard_snapshot(session, project_id=root.project_id)["selected_project"]["name"] == "Nhà hàng An Nhiên"


def test_project_view_isolates_agents_files_events_and_shared_agent_work(dashboard):
    with dashboard["factory"]() as session:
        session.get(TaskModel, "DEV-1").dispatch_state = "QUEUED"
        session.get(WorkerModel, "worker-4").last_heartbeat_at = utc_now()
        session.add(TaskModel(task_id="OTHER-DEV", run_id="RUN-OTHER", agent_id="dev-01", role="DEV", action="implement_feature",
            instruction="Other project private work", dispatch_state="RUNNING", context={"title": "PRIVATE-OTHER-WORK"}))
        session.flush()
        other_file = dashboard["store"].save_artifact(session, b"Other project's file", "private-other-file.md", "RUN-OTHER", "PM-LOCAL", "REQUIREMENT", "pm-01")
        session.add(AuditLogModel(action="TASK_COMPLETED", actor="pm-01", target="PRIVATE-OTHER-AUDIT", run_id="RUN-OTHER", result="SUCCESS"))
        session.add(AgentEventModel(event_id="PRIVATE-OTHER-EVENT", event_type="TASK_STARTED", run_id="RUN-OTHER", task_id="PM-LOCAL", payload={}))
        session.add(TaskDependencyModel(task_id="TEST-1", depends_on_task_id="PM-LOCAL"))
        session.commit()
        scoped = dashboard_snapshot(session, project_id="LIB", view="project")
        assert {a["agent_id"] for a in scoped["agents"]} == {"ba-01", "dev-01", "tester-01"}
        dev = next(a for a in scoped["agents"] if a["agent_id"] == "dev-01")
        assert dev["task"]["run_id"] == "RUN-DASH" and dev["status"] == "WAITING_AGENT"
        assert scoped["summary"]["files_received"] == 1
        assert {a["run_id"] for a in [*scoped["artifacts"], *scoped["recent_artifacts"]]} == {"RUN-DASH"}
        assert all(e["from"] != "PM-LOCAL" for e in scoped["edges"])
        assert "PRIVATE-OTHER" not in json.dumps(scoped["activity"])
        assert other_file.artifact_id not in {a["artifact_id"] for a in scoped["recent_artifacts"]}
        assert all(a["task"] is None or a["task"]["run_id"] == "RUN-DASH" for a in scoped["agents"])
        system = dashboard_snapshot(session, project_id="LIB", view="system")
        assert len(system["agents"]) == 10 and system["summary"]["files_received"] == 2
        assert next(a for a in system["agents"] if a["agent_id"] == "dev-01")["task"]["title"] == "PRIVATE-OTHER-WORK"
        with pytest.raises(LookupError):
            dashboard_snapshot(session, "RUN-OTHER", project_id="LIB", view="project")


@pytest.mark.asyncio
async def test_rename_project_is_authenticated_and_preserves_run_ids_and_files(dashboard):
    url = "/api/v1/projects/LIB"
    with dashboard["factory"]() as session:
        before = dashboard_snapshot(session, project_id="LIB")
        original_path = before["artifacts"][0]["local_path"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard["app"]), base_url="http://test") as client:
        assert (await client.patch(url, json={"name": "Quản lý nhà hàng"})).status_code == 401
        renamed = await client.patch(url, json={"name": "Quản lý nhà hàng"}, headers=dashboard["headers"])
        assert renamed.status_code == 200 and renamed.json()["project_id"] == "LIB"
        data = (await client.get("/api/v1/dashboard/snapshot?project_id=LIB&view=project", headers=dashboard["headers"])).json()
        assert data["selected_project"]["name"] == "Quản lý nhà hàng" and data["selected_run"]["run_id"] == "RUN-DASH"
        assert data["artifacts"][0]["local_path"] == original_path
        assert (await client.patch(url, json={"name": "   "}, headers=dashboard["headers"])).status_code == 422
        assert (await client.patch("/api/v1/projects/MISSING", json={"name": "Tên mới"}, headers=dashboard["headers"])).status_code == 404
        from control.app.security.authentication import WorkerPrincipal
        dashboard["app"].dependency_overrides[get_current_principal] = lambda: WorkerPrincipal("worker-3", "PC3", ["ba-01"])
        assert (await client.patch(url, json={"name": "Not allowed"})).status_code == 403


@pytest.mark.asyncio
async def test_new_project_accepts_an_explicit_display_name(dashboard, monkeypatch):
    from control.app.api.dependencies import get_publisher
    monkeypatch.setattr(settings, "MAX_CONCURRENT_RUNS", 10)
    dashboard["app"].dependency_overrides[get_publisher] = lambda: None
    proposal = {"project_name": "Restaurant management", "overview": "Quản lý thực đơn", "modules": [
        {"module_id": "menu", "name": "Thực đơn", "description": "Quản lý món ăn", "tasks": [
            {"task_id": "add-dish", "title": "Thêm món", "description": "Thêm món vào thực đơn", "acceptance_criteria": ["Món được lưu"]}]}]}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=dashboard["app"]), base_url="http://test") as client:
        created = await client.post("/api/v1/runs", headers=dashboard["headers"], json={"run_id": "RUN-REST-NAME", "project_id": "PRJ-REST",
            "project_name": "Quản lý nhà hàng Bếp Việt", "brief": "Xây dựng hệ thống quản lý nhà hàng", "module_plan": proposal})
        assert created.status_code == 201, created.text
        data = (await client.get("/api/v1/dashboard/snapshot?project_id=PRJ-REST&view=project", headers=dashboard["headers"])).json()
        assert data["selected_project"]["name"] == "Quản lý nhà hàng Bếp Việt"
        assert all(task["run_id"] == "RUN-REST-NAME" for task in data["tasks"])
