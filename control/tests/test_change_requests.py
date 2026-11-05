"""End-to-end changes against real, immutable local artifact files and fake Linear."""

from copy import deepcopy
import json

import pytest
from sqlalchemy import create_engine, select, event, func
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent_contract.project_plan import compile_module_plan
from control.app.config import settings
from control.app.database.models import Base, ArtifactModel, ProjectModel, ProjectReleaseModel, TaskModel, WorkflowRunModel
from control.app.database.repositories import WorkflowRunRepository
from control.app.integrations.linear.client import FakeWorkTracker
from control.app.integrations.linear.config_manager import initialize_and_validate_linear_schema, DEFAULT_CONFIG
from control.app.integrations.linear.sync import LinearSyncService
from control.app.services.approval import ApprovalService
from control.app.services.artifact_store import ArtifactStoreService, ArtifactStoreError, select_input_artifacts, model_to_artifact_ref
from control.app.services.change_requests import ChangeRequestService, ChangeRequestError
from control.app.services.plan_service import PlanService, build_default_plan
from control.app.services.project_coordinator import ProjectCoordinator
from control.app.services.task_manager import TaskManager


def initial_plan():
    return {"project_name": "Library", "overview": "Book catalog and lending", "modules": [
        {"module_id": "books", "name": "Books", "description": "Catalog", "tasks": [
            {"task_id": "search", "title": "Search books", "description": "Search catalog", "acceptance_criteria": ["Search finds books"]}]},
        {"module_id": "loans", "name": "Loans", "description": "Lending", "tasks": [
            {"task_id": "borrow", "title": "Borrow books", "description": "Borrow an available book", "acceptance_criteria": ["Loan is recorded"]}]}]}


def change_plan(**flags):
    return {"project_name": "Library", "overview": "Allow renewal twice", "impact_summary": "Only lending changes",
        **flags, "modules": [{"module_id": "loans", "name": "Loans", "description": "Add renewal to existing loans", "tasks": [
            {"task_id": "renew", "title": "Renew loan", "description": "Allow at most two renewals of an active loan",
             "acceptance_criteria": ["First and second renewals succeed", "Third renewal is rejected"]}]}]}


class PhysicalDispatcher:
    def __init__(self, store):
        self.store, self.calls, self.failures = store, [], 0
        self.security_failures = 0

    def dispatch_single_task(self, session, task):
        inputs = select_input_artifacts(session, task)
        self.calls.append((task.run_id, task.action, deepcopy(task.context), inputs))
        payload = {"instruction": task.instruction, "module_id": (task.context or {}).get("module_id")}
        if (task.context or {}).get("output_artifact_type") == "SOURCE_CODE":
            code = next((a for a in inputs if a.type.value == "SOURCE_CODE"), None)
            if code:
                path, _ = self.store.get_artifact_file_path(session, code.artifact_id)
                payload = json.loads(path.read_text())
            else:
                payload = {"features": []}
            if task.action == "implement_feature":
                payload["features"].append(f"{task.context['module_id']}:{task.context['work_item_id']}")
        quality = task.action in {"run_test_suite", "security_review"}
        verdict = "PASS"
        if task.action == "run_test_suite" and self.failures:
            self.failures -= 1
            verdict = "FAIL"
        if task.action == "security_review" and self.security_failures:
            self.security_failures -= 1
            verdict = "FAIL"
        artifacts = []
        kind = (task.context or {}).get("output_artifact_type")
        if kind:
            artifact = self.store.save_artifact(session, json.dumps(payload).encode(), f"{kind.lower()}.json",
                task.run_id, task.task_id, kind, task.agent_id)
            artifacts.append(model_to_artifact_ref(artifact).model_dump(mode="json"))
        task.dispatch_state = "COMPLETED"
        task.result = {"status": "COMPLETED", "summary": "Executed", "outputs": [{"verdict": verdict}] if quality else [],
                       "artifacts": artifacts}
        session.commit()
        return True


def drive(env, run_id, limit=160):
    for _ in range(limit):
        env["coordinator"].tick(run_id)
        with env["factory"]() as session:
            run = session.get(WorkflowRunModel, run_id)
            if run.status in {"COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}:
                return run
            if run.status == "AWAITING_APPROVAL":
                gate = next((t.action for t in TaskManager.get_ready_tasks(session, run_id) if t.role == "GATE"), "APPROVE_PLAN")
                ApprovalService().approve(session, run_id, gate=gate, decided_by="offline-test")
    pytest.fail(f"Run {run_id} did not stabilize")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PM_AUTO_RECOVERY", False)
    monkeypatch.setattr(settings, "APPROVAL_MODE", False)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    @event.listens_for(engine, "connect")
    def foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    tracker = FakeWorkTracker()
    cache = initialize_and_validate_linear_schema(tracker, "test-team", DEFAULT_CONFIG)
    linear = LinearSyncService(tracker, cache, session_factory=factory)
    linear._throttle = lambda: None
    store = ArtifactStoreService(tmp_path / "store")
    dispatcher = PhysicalDispatcher(store)
    env = {"factory": factory, "linear": linear, "tracker": tracker, "store": store, "dispatcher": dispatcher,
           "service": ChangeRequestService(store), "coordinator": ProjectCoordinator(factory, dispatcher, linear, store)}
    with factory() as session:
        WorkflowRunRepository.create_run(session, {"run_id": "RUN-BASE", "project_id": "LIB", "brief": "Build library"})
        plan = compile_module_plan(build_default_plan("Build library", "LIB"), initial_plan())
        PlanService().save_plan_to_db(session, "RUN-BASE", plan)
    result = drive(env, "RUN-BASE")
    assert result.status == "COMPLETED", result.run_metadata
    yield env
    engine.dispose()


def request(env, proposal=None, base_run_id=None):
    with env["factory"]() as session:
        return env["service"].create(session, "LIB", "Allow at most two renewals", "offline-test", proposal, base_run_id)


def test_change_extends_exact_source_keeps_history_and_same_linear_project(project):
    with project["factory"]() as session:
        base = session.scalar(select(ProjectReleaseModel).where(ProjectReleaseModel.run_id == "RUN-BASE"))
        old_manifest = deepcopy(base.manifest)
        old_tasks = [(t.task_id, t.dispatch_state, deepcopy(t.context), deepcopy(t.result)) for t in
                     session.scalars(select(TaskModel).where(TaskModel.run_id == "RUN-BASE"))]
        old_files = {a["artifact_id"]: project["store"].get_artifact_file_path(session, a["artifact_id"])[0].read_bytes()
                     for a in base.manifest["artifacts"]}
    change = request(project, change_plan())
    with project["factory"]() as session:
        assert session.get(ProjectModel, "LIB").current_release_id == base.release_id
        tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id == change.run_id)))
        assert not any(t.action in {"implement_project", "design_architecture", "design_schema"} for t in tasks)
        assert {t.context["module_id"] for t in tasks if t.action == "implement_feature"} == {"loans"}
    result = drive(project, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    calls = [c for c in project["dispatcher"].calls if c[0] == change.run_id]
    first_dev = next(c for c in calls if c[1] == "implement_feature")
    baseline_code = next(a for a in old_manifest["artifacts"] if a["type"] == "SOURCE_CODE")
    assert [a.artifact_id for a in first_dev[3] if a.type.value == "SOURCE_CODE"] == [baseline_code["artifact_id"]]
    regression = next(c for c in calls if c[2].get("title") == "Full project regression")
    old_book_tests = {a["artifact_id"] for a in old_manifest["artifacts"] if a["type"] == "TEST_CASE" and a["module_id"] == "books"}
    assert old_book_tests <= {a.artifact_id for a in regression[3]}
    with project["factory"]() as session:
        current = session.get(ProjectModel, "LIB")
        assert current.release_counter == 2 and current.active_change_run_id is None
        release = session.get(ProjectReleaseModel, current.current_release_id)
        assert release.base_release_id == base.release_id
        code = next(a for a in release.manifest["artifacts"] if a["type"] == "SOURCE_CODE")
        path, _ = project["store"].get_artifact_file_path(session, code["artifact_id"])
        assert set(json.loads(path.read_text())["features"]) == {"books:search", "loans:borrow", "loans:renew"}
        assert session.get(ProjectReleaseModel, base.release_id).manifest == old_manifest
        assert old_tasks == [(t.task_id, t.dispatch_state, t.context, t.result) for t in
                             session.scalars(select(TaskModel).where(TaskModel.run_id == "RUN-BASE"))]
        for artifact_id, content in old_files.items():
            assert project["store"].get_artifact_file_path(session, artifact_id)[0].read_bytes() == content
    assert len(project["tracker"].projects) == 1


def test_completed_tasks_are_done_on_linear_and_files_are_handed_to_the_next_role(project):
    from control.app.database.repositories import LinearLinkRepository
    with project["factory"]() as session:
        tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id == "RUN-BASE")))
        for task in tasks:
            issue = LinearLinkRepository.get_link(session, "TASK", task.task_id)
            assert project["tracker"].issues[issue.linear_id]["state_id"] == project["linear"].schema_cache.dispatch_to_state_id["COMPLETED"]
        code_call = next(c for c in project["dispatcher"].calls if c[1] == "implement_feature")
        test_call = next(c for c in project["dispatcher"].calls if c[1] == "run_test_suite")
        assert any(ref.type.value == "REQUIREMENT" for ref in code_call[3])
        produced_code = session.scalar(select(ArtifactModel).where(ArtifactModel.task_id == test_call[2]["input_artifact_sources"]["SOURCE_CODE"][0],
                                                                   ArtifactModel.type == "SOURCE_CODE"))
        assert [ref.artifact_id for ref in test_call[3] if ref.type.value == "SOURCE_CODE"] == [produced_code.artifact_id]
        for ref in [*code_call[3], *test_call[3]]:
            file_path, _ = project["store"].get_artifact_file_path(session, ref.artifact_id)
            import hashlib
            assert hashlib.sha256(file_path.read_bytes()).hexdigest() == ref.checksum


def test_changes_can_chain_and_cannot_overwrite_an_older_baseline(project):
    first = request(project, change_plan())
    assert drive(project, first.run_id).status == "COMPLETED"
    with pytest.raises(ChangeRequestError, match="current accepted version"):
        request(project, change_plan(), "RUN-BASE")
    second = request(project, change_plan())
    assert second.run_metadata["base_run_id"] == first.run_id
    assert drive(project, second.run_id).status == "COMPLETED"
    with project["factory"]() as session:
        assert session.get(ProjectModel, "LIB").release_counter == 3


def test_failed_change_never_promotes_current_version(project, monkeypatch):
    change = request(project, change_plan())
    monkeypatch.setattr(settings, "MAX_FIX_LOOPS", 0)
    project["dispatcher"].failures = 1
    assert drive(project, change.run_id).status == "BLOCKED"
    with project["factory"]() as session:
        head = session.get(ProjectModel, "LIB")
        assert head.release_counter == 1
        assert session.get(ProjectReleaseModel, head.current_release_id).run_id == "RUN-BASE"


def test_security_repair_retests_both_changed_and_unchanged_modules(project):
    change = request(project, change_plan())
    project["dispatcher"].security_failures = 1
    result = drive(project, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    calls = [call for call in project["dispatcher"].calls if call[0] == change.run_id]
    fix = next(i for i, call in enumerate(calls) if call[1] == "fix_bugs")
    retest = calls[fix + 1]
    assert retest[1] == "run_test_suite"
    with project["factory"]() as session:
        pinned = session.scalar(select(ProjectReleaseModel).where(ProjectReleaseModel.run_id == "RUN-BASE")).manifest["artifacts"]
    book_tests = {a["artifact_id"] for a in pinned if a["type"] == "TEST_CASE" and a["module_id"] == "books"}
    assert book_tests <= {a.artifact_id for a in retest[3]}
    assert calls[fix + 2][1] == "security_review"


def test_only_one_active_change_can_reserve_a_project(project):
    first = request(project, change_plan())
    with pytest.raises(ChangeRequestError, match="active change"):
        request(project, change_plan())
    with project["factory"]() as session:
        session.get(WorkflowRunModel, first.run_id).status = "CANCELLED"
        session.commit()
    second = request(project, change_plan())
    assert second.run_id != first.run_id


def test_missing_or_tampered_baseline_is_rejected_before_creating_tasks(project):
    with project["factory"]() as session:
        release = session.scalar(select(ProjectReleaseModel))
        source = next(a for a in release.manifest["artifacts"] if a["type"] == "SOURCE_CODE")
        path, _ = project["store"].get_artifact_file_path(session, source["artifact_id"])
        path.write_bytes(b"tampered source")
    with pytest.raises(ChangeRequestError, match="checksum mismatch"):
        request(project, change_plan())
    with project["factory"]() as session:
        assert session.scalar(select(func.count()).select_from(WorkflowRunModel)) == 1


def test_unrelated_artifact_cannot_be_injected_into_baseline(project):
    change = request(project, change_plan())
    with project["factory"]() as session:
        WorkflowRunRepository.create_run(session, {"run_id": "OTHER", "project_id": "OTHER", "brief": "Other"})
        artifact = project["store"].save_artifact(session, b"{}", "other.json", "OTHER", None, "SOURCE_CODE", "dev-01")
        task = session.scalar(select(TaskModel).where(TaskModel.run_id == change.run_id, TaskModel.action == "implement_feature"))
        task.context = {"input_artifact_types": ["SOURCE_CODE"], "baseline_artifact_ids": {"SOURCE_CODE": [artifact.artifact_id]}}
        with pytest.raises(ArtifactStoreError, match="not a pinned"):
            select_input_artifacts(session, task)


def test_design_revisions_are_scheduled_only_when_requested(project):
    change = request(project, change_plan(architecture_changed=True, database_changed=True))
    with project["factory"]() as session:
        actions = {t.action for t in session.scalars(select(TaskModel).where(TaskModel.run_id == change.run_id))}
        assert {"design_architecture", "design_schema"} <= actions
    result = drive(project, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata


def test_pm_receives_existing_documents_and_module_ids_before_change_planning(project):
    run = request(project)
    captured = []
    def pm(prompt, project_id, errors, context):
        captured.append(context)
        return {"module_plan": change_plan()}
    with project["factory"]() as session:
        plan = project["service"].plan(session, run.run_id, pm)
    assert plan["run_kind"] == "change"
    assert captured[0]["change_request"] is True
    assert {m["module_id"] for m in captured[0]["baseline_modules"]} == {"books", "loans"}
    assert any(d["type"] == "REQUIREMENT" and d["content"] for d in captured[0]["baseline_documents"])


def test_change_planning_retry_is_idempotent_after_restart(project):
    run = request(project)
    calls = []
    def pm(*args):
        calls.append(args)
        return {"module_plan": change_plan()}
    with project["factory"]() as session:
        project["service"].plan(session, run.run_id, pm)
        count = session.scalar(select(func.count()).select_from(TaskModel).where(TaskModel.run_id == run.run_id))
    with project["factory"]() as session:
        ChangeRequestService(project["store"]).plan(session, run.run_id, pm)
        assert session.scalar(select(func.count()).select_from(TaskModel).where(TaskModel.run_id == run.run_id)) == count
    assert len(calls) == 1


def test_release_publication_rechecks_after_waiting_for_project_lock(project, monkeypatch):
    from control.app.services.project_releases import publish_release
    with project["factory"]() as session:
        run = session.get(WorkflowRunModel, "RUN-BASE")
        real_scalar = session.scalar
        calls = []
        def stale_first_read(statement, *args, **kwargs):
            calls.append(statement)
            # Simulate publication by another coordinator between the first read and project lock.
            return None if len(calls) == 1 else real_scalar(statement, *args, **kwargs)
        monkeypatch.setattr(session, "scalar", stale_first_read)
        release = publish_release(session, run)
        assert release.version == 1
        assert session.get(ProjectModel, "LIB").release_counter == 1


@pytest.mark.asyncio
async def test_runtime_recovers_a_change_left_in_planning(project, monkeypatch):
    import asyncio
    import threading
    from control.app.services import project_runtime, change_requests
    change = request(project)
    recovered = threading.Event()
    def recover(run_id):
        with project["factory"]() as session:
            project["service"].plan(session, run_id, lambda *args: {"module_plan": change_plan()})
        recovered.set()
    monkeypatch.setattr(project_runtime, "get_session_factory", lambda: project["factory"])
    monkeypatch.setattr(project_runtime, "DispatcherService", lambda **kwargs: project["dispatcher"])
    monkeypatch.setattr(project_runtime, "LinearSyncService", lambda *args: project["linear"])
    monkeypatch.setattr(project_runtime, "initialize_and_validate_linear_schema", lambda *args: project["linear"].schema_cache)
    monkeypatch.setattr(project_runtime, "LinearPollingService", lambda *args: type("Polling", (), {"poll_once": lambda self: None})())
    monkeypatch.setattr(change_requests, "plan_change_in_background", recover)
    monkeypatch.setattr(settings, "LINEAR_DRY_RUN", True)
    stop = asyncio.Event()
    runtime = asyncio.create_task(project_runtime.run_project_loop(stop, None, None))
    try:
        assert await asyncio.to_thread(recovered.wait, 5)
    finally:
        stop.set()
        await asyncio.wait_for(runtime, 5)
    with project["factory"]() as session:
        assert session.get(WorkflowRunModel, change.run_id).status == "RUNNING"
        assert session.scalar(select(TaskModel.task_id).where(TaskModel.run_id == change.run_id).limit(1))


def test_completed_pre_upgrade_run_is_adopted_without_rebuilding(project):
    from sqlalchemy import delete
    with project["factory"]() as session:
        session.execute(delete(ProjectReleaseModel))
        session.execute(delete(ProjectModel))
        run = session.get(WorkflowRunModel, "RUN-BASE")
        run.run_metadata = {key: value for key, value in run.run_metadata.items() if key not in {"release_id", "release_version"}}
        session.commit()
    change = request(project, change_plan())
    assert change.run_metadata["base_run_id"] == "RUN-BASE"
    with project["factory"]() as session:
        assert session.get(ProjectModel, "LIB").release_counter == 1
        assert session.get(WorkflowRunModel, "RUN-BASE").status == "COMPLETED"
    assert drive(project, change.run_id).status == "COMPLETED"


def test_failed_planning_requires_review_and_keeps_current_release(project):
    run = request(project)
    attempts = []
    def broken(*args):
        attempts.append(args)
        raise ValueError("invalid planning output")
    with project["factory"]() as session:
        plan = project["service"].plan(session, run.run_id, broken)
        assert plan["planning_warning"]
        assert session.get(WorkflowRunModel, run.run_id).status == "AWAITING_APPROVAL"
        assert session.get(ProjectModel, "LIB").release_counter == 1
    assert len(attempts) == 3


@pytest.mark.asyncio
async def test_api_requires_auth_and_exposes_change_and_version_history(project, monkeypatch):
    import httpx
    from fastapi import FastAPI
    from control.app.api import api_v1_router
    from control.app.api.dependencies import get_db, get_publisher
    from control.app.api import projects as api_projects
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "offline-admin-token")
    monkeypatch.setattr(api_projects, "ChangeRequestService", lambda **kwargs: project["service"])
    app = FastAPI()
    app.include_router(api_v1_router)
    from control.app.main import security_exception_handler
    from control.app.security.permissions import SecurityException
    app.add_exception_handler(SecurityException, security_exception_handler)
    def db():
        with project["factory"]() as session:
            yield session
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[get_publisher] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        payload = {"brief": "Add loan renewal", "module_plan": change_plan()}
        assert (await client.post("/api/v1/projects/LIB/changes", json=payload)).status_code == 401
        headers = {"Authorization": "Bearer offline-admin-token"}
        response = await client.post("/api/v1/projects/LIB/changes", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        result = response.json()
        assert result["target_version"] == 2 and result["base_run_id"] == "RUN-BASE"
        head = (await client.get("/api/v1/projects/LIB", headers=headers)).json()
        assert head["current_version"] == 1 and head["active_change_run_id"] == result["run_id"]
        duplicate = await client.post("/api/v1/projects/LIB/changes", json=payload, headers=headers)
        assert duplicate.status_code == 409
        assert drive(project, result["run_id"]).status == "COMPLETED"
        releases = (await client.get("/api/v1/projects/LIB/releases", headers=headers)).json()
        assert [r["version"] for r in releases] == [2, 1]
        latest = await client.get(f"/api/v1/projects/LIB/releases/{releases[0]['release_id']}", headers=headers)
        assert latest.json()["manifest"]["artifacts"]


def test_project_release_migration_can_be_retried_without_losing_existing_tables():
    import importlib.util
    from pathlib import Path
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect
    path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "002_project_releases.py"
    spec = importlib.util.spec_from_file_location("project_release_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    old_tables = [t for t in Base.metadata.sorted_tables if t.name not in {"projects", "project_releases"}]
    Base.metadata.create_all(engine, tables=old_tables)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        migration.upgrade()
        assert {"workflow_runs", "projects", "project_releases"} <= set(inspect(connection).get_table_names())
        migration.downgrade()
        assert "workflow_runs" in inspect(connection).get_table_names()
        assert "project_releases" not in inspect(connection).get_table_names()
    engine.dispose()
