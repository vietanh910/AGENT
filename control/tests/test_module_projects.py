"""Offline acceptance tests for the project -> module -> task lifecycle."""

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from agent_contract.project_plan import ModulePlan, compile_module_plan
from control.app.config import settings
from control.app.database.models import Base, TaskModel, WorkflowRunModel
from control.app.database.repositories import ArtifactRepository, WorkflowRunRepository
from control.app.integrations.linear.client import FakeWorkTracker
from control.app.integrations.linear.config_manager import initialize_and_validate_linear_schema, DEFAULT_CONFIG
from control.app.integrations.linear.sync import LinearSyncService
from control.app.services.artifact_store import ArtifactStoreError, select_input_artifacts
from control.app.services.plan_service import PlanService, build_default_plan
from control.app.services.plan_validator import PlanValidator
from control.app.services.project_coordinator import ProjectCoordinator
from control.app.services.task_manager import TaskManager


def proposal():
    return {"project_name": "Library", "overview": "Books and borrowing", "modules": [
        {"module_id": "books", "name": "Books", "description": "Manage catalog", "tasks": [
            {"task_id": "create", "title": "Create books", "description": "Create catalog entries", "acceptance_criteria": ["Book persists"]},
            {"task_id": "search", "title": "Search books", "description": "Search catalog", "acceptance_criteria": ["Search finds book"]}]},
        {"module_id": "loans", "name": "Loans", "description": "Manage borrowing", "tasks": [
            {"task_id": "borrow", "title": "Borrow", "description": "Borrow a catalog book", "acceptance_criteria": ["Loan is recorded"]}]}]}


@pytest.fixture
def setup_project(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "PM_AUTO_RECOVERY", False)
    monkeypatch.setattr("control.app.services.artifact_store.DEFAULT_ARTIFACT_STORE_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(settings, "APPROVAL_MODE", False)
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    tracker = FakeWorkTracker()
    cache = initialize_and_validate_linear_schema(tracker, "team-test", DEFAULT_CONFIG)
    cache.max_req_per_min = 10000000
    linear = LinearSyncService(tracker, cache, session_factory=factory)
    linear._throttle = lambda: None
    plan = compile_module_plan(build_default_plan("Build library software", "LIB"), proposal())
    with factory() as session:
        WorkflowRunRepository.create_run(session, {"run_id": "RUN-MOD-01", "project_id": "LIB", "brief": "Library"})
        PlanService().save_plan_to_db(session, "RUN-MOD-01", plan)
    yield factory, linear, tracker, plan
    engine.dispose()


class CompletingDispatcher:
    def __init__(self):
        self.calls = []
        self.failures = 0
        self.security_failures = 0

    def dispatch_single_task(self, session, task):
        inputs = select_input_artifacts(session, task)
        self.calls.append((task.task_id, task.action, task.context, inputs))
        quality = task.action in {"run_test_suite", "security_review"}
        verdict = "PASS"
        if task.action == "run_test_suite" and self.failures:
            self.failures -= 1
            verdict = "FAIL"
        if task.action == "security_review" and self.security_failures:
            self.security_failures -= 1
            verdict = "FAIL"
        expected = (task.context or {}).get("output_artifact_type")
        artifacts = []
        if expected:
            art_id = "ART-" + task.task_id
            model = ArtifactRepository.save_artifact(session, {
                "artifact_id": art_id, "run_id": task.run_id, "task_id": task.task_id,
                "name": expected + ".json", "type": expected, "uri": f"artifact://{art_id}", "created_by": task.agent_id})
            artifacts = [{"artifact_id": art_id, "name": model.name, "type": expected, "uri": model.uri}]
        task.dispatch_state = "COMPLETED"
        task.result = {"status": "COMPLETED", "outputs": [{"verdict": verdict}] if quality else [], "artifacts": artifacts}
        session.commit()
        return True


def drive(factory, coordinator, limit=100):
    for _ in range(limit):
        coordinator.tick("RUN-MOD-01")
        with factory() as session:
            run = session.get(WorkflowRunModel, "RUN-MOD-01")
            if run.status in {"COMPLETED", "BLOCKED"}:
                return run
            if run.status == "AWAITING_APPROVAL":
                from control.app.services.approval import ApprovalService
                ready = TaskManager.get_ready_tasks(session, run.run_id)
                gate = next((t.action for t in ready if t.role == "GATE"), "APPROVE_PLAN")
                ApprovalService().approve(session, run.run_id, gate=gate, decided_by="test")
    pytest.fail("Project did not reach a terminal state")


def test_full_backlog_is_compiled_and_valid_before_execution():
    plan = compile_module_plan(build_default_plan("Library", "LIB"), proposal())
    validation = PlanValidator().validate(plan)
    assert validation.valid, validation.errors
    assert [t["title"] for t in plan["tasks"] if t["action"] == "implement_feature"] == ["Create books", "Search books", "Borrow"]
    assert len([t for t in plan["tasks"] if t["action"] == "run_test_suite"]) == 3
    assert plan["modules"] == proposal()["modules"]


def test_linear_barrier_blocks_all_dispatch_on_outage(setup_project):
    factory, linear, tracker, plan = setup_project
    linear.sync_entity_now = lambda *a, **kw: None
    dispatcher = CompletingDispatcher()
    coordinator = ProjectCoordinator(factory, dispatcher, linear)
    for _ in range(3):
        coordinator.tick("RUN-MOD-01")
    assert not dispatcher.calls


def test_sequential_modules_reuse_srs_and_exact_code_after_restart(setup_project):
    factory, linear, tracker, plan = setup_project
    dispatcher = CompletingDispatcher()
    coordinator = ProjectCoordinator(factory, dispatcher, linear)
    for _ in range(12):
        coordinator.tick("RUN-MOD-01")
    # A new coordinator has no in-memory progress to recover; the DB is authoritative.
    coordinator = ProjectCoordinator(factory, dispatcher, linear)
    result = drive(factory, coordinator)
    assert result.status == "COMPLETED", result.run_metadata
    feature_calls = [call for call in dispatcher.calls if call[1] == "implement_feature"]
    assert [call[2]["module_id"] for call in feature_calls] == ["books", "books", "loans"]
    first_srs = {r.artifact_id for r in feature_calls[0][3] if r.type.value == "REQUIREMENT"}
    second_srs = {r.artifact_id for r in feature_calls[1][3] if r.type.value == "REQUIREMENT"}
    assert len(first_srs) == 2 and first_srs == second_srs
    for tid, action, context, refs in dispatcher.calls:
        if action == "run_test_suite":
            expected = context["input_artifact_sources"]["SOURCE_CODE"][0]
            assert [r.artifact_id for r in refs if r.type.value == "SOURCE_CODE"] == ["ART-" + expected]
    assert len(tracker.projects) == 1
    assert any(issue["milestone_id"] for issue in tracker.issues.values())


def test_failed_test_repairs_and_retests_before_next_feature(setup_project):
    factory, linear, tracker, plan = setup_project
    dispatcher = CompletingDispatcher()
    dispatcher.failures = 1
    result = drive(factory, ProjectCoordinator(factory, dispatcher, linear))
    assert result.status == "COMPLETED", result.run_metadata
    actions = [c[1] for c in dispatcher.calls]
    index = actions.index("fix_bugs")
    assert actions[index + 1] == "run_test_suite"
    next_feature = next(c for c in dispatcher.calls[index + 1:] if c[1] == "implement_feature")
    assert any(ref.artifact_id == "ART-" + dispatcher.calls[index][0] for ref in next_feature[3])
    assert result.run_metadata["fix_loop_count"] == 1


def test_fix_loop_limit_blocks_project(setup_project, monkeypatch):
    factory, linear, tracker, plan = setup_project
    monkeypatch.setattr(settings, "MAX_FIX_LOOPS", 1)
    dispatcher = CompletingDispatcher()
    dispatcher.failures = 20
    result = drive(factory, ProjectCoordinator(factory, dispatcher, linear))
    assert result.status == "BLOCKED"
    assert "MAX_FIX_LOOPS=1" in result.run_metadata["block_reason"]
    assert not any(c[2].get("work_item_id") == "search" for c in dispatcher.calls)


def test_security_failure_requires_retest_and_security_recheck(setup_project):
    factory, linear, tracker, plan = setup_project
    dispatcher = CompletingDispatcher()
    dispatcher.security_failures = 1
    result = drive(factory, ProjectCoordinator(factory, dispatcher, linear))
    assert result.status == "COMPLETED", result.run_metadata
    actions = [call[1] for call in dispatcher.calls]
    fix = actions.index("fix_bugs")
    assert actions[fix + 1:fix + 3] == ["run_test_suite", "security_review"]
    assert any(ref.type.value == "TEST_CASE" for ref in dispatcher.calls[fix + 1][3])


def test_missing_bound_artifact_never_uses_another_modules_output(setup_project):
    factory, linear, tracker, plan = setup_project
    with factory() as session:
        source = session.get(TaskModel, "RUN-MOD-01_m01_t01_dev")
        source.dispatch_state = "COMPLETED"
        ArtifactRepository.save_artifact(session, {"artifact_id": "ART-UNRELATED", "run_id": source.run_id,
            "task_id": "RUN-MOD-01_m02_t01_dev", "name": "code.zip", "type": "SOURCE_CODE",
            "uri": "artifact://ART-UNRELATED", "created_by": "dev-01"})
        task = session.get(TaskModel, "RUN-MOD-01_m01_t01_test")
        task.context = {"input_artifact_types": ["SOURCE_CODE"], "input_artifact_sources": {"SOURCE_CODE": [source.task_id]}}
        with pytest.raises(ArtifactStoreError, match="Missing SOURCE_CODE"):
            select_input_artifacts(session, task)


def test_duplicate_business_ids_rejected():
    data = proposal()
    data["modules"][1]["module_id"] = "books"
    with pytest.raises(ValueError, match="Duplicate module_id"):
        ModulePlan.model_validate(data)


def test_tampered_cross_module_input_is_rejected():
    plan = compile_module_plan(build_default_plan("Library", "LIB"), proposal())
    task = next(t for t in plan["tasks"] if t["task_id"] == "m01_t01_test")
    task["context"]["input_artifact_sources"]["SOURCE_CODE"] = ["m02_t01_dev"]
    result = PlanValidator().validate(plan)
    assert not result.valid
    assert any("non-ancestor producer" in error for error in result.errors)


def test_pm_modules_are_used_instead_of_discarded(monkeypatch):
    from agent_contract.models import TaskResult
    from agent_contract.enums import TaskStatus
    class PM:
        def __init__(self, *args):
            pass
        def handle(self, request):
            assert request.context["execution_plan"] is True
            return TaskResult(task_id=request.task_id, agent_id="pm-01", status=TaskStatus.COMPLETED,
                              summary="Plan", outputs=[{"module_plan": proposal()}])
    monkeypatch.setattr("worker.host.process_agent.ProcessAgent", PM)
    plan = PlanService()._call_pm_agent("Build library project", "LIB")
    assert plan["modules"] == proposal()["modules"]
    assert len([t for t in plan["tasks"] if t["action"] == "implement_feature"]) == 3


def test_missing_completed_output_blocks_next_task(setup_project):
    factory, linear, tracker, plan = setup_project
    dispatcher = CompletingDispatcher()
    coordinator = ProjectCoordinator(factory, dispatcher, linear)
    with factory() as session:
        task = session.get(TaskModel, "RUN-MOD-01_po_define_backlog")
        task.dispatch_state = "COMPLETED"
        task.result = {"status": "COMPLETED", "outputs": []}
        session.commit()
    for _ in range(20):
        coordinator.tick("RUN-MOD-01")
    with factory() as session:
        run = session.get(WorkflowRunModel, "RUN-MOD-01")
        assert run.status == "BLOCKED"
        assert "Missing BACKLOG" in run.run_metadata["block_reason"]
        from control.app.database.repositories import LinearLinkRepository
        link = LinearLinkRepository.get_link(session, "TASK", "RUN-MOD-01_po_define_backlog")
        assert tracker.issues[link.linear_id]["state_id"] != linear.schema_cache.dispatch_to_state_id["COMPLETED"]
    assert not dispatcher.calls


def test_quality_check_without_verdict_is_not_done_on_linear(setup_project):
    factory, linear, tracker, plan = setup_project
    dispatcher = CompletingDispatcher()
    coordinator = ProjectCoordinator(factory, dispatcher, linear)
    with factory() as session:
        task = session.get(TaskModel, "RUN-MOD-01_m01_t01_test")
        artifact = ArtifactRepository.save_artifact(session, {"artifact_id": "ART-NO-VERDICT", "run_id": task.run_id,
            "task_id": task.task_id, "name": "report.json", "type": "TEST_REPORT", "uri": "artifact://ART-NO-VERDICT",
            "created_by": task.agent_id})
        task.dispatch_state = "COMPLETED"
        task.result = {"status": "COMPLETED", "outputs": [], "artifacts": [{"artifact_id": artifact.artifact_id, "type": artifact.type}]}
        session.commit()
    for _ in range(20):
        coordinator.tick("RUN-MOD-01")
    with factory() as session:
        from control.app.database.repositories import LinearLinkRepository
        run = session.get(WorkflowRunModel, "RUN-MOD-01")
        assert run.status == "BLOCKED" and "Missing quality verdict" in run.run_metadata["block_reason"]
        link = LinearLinkRepository.get_link(session, "TASK", "RUN-MOD-01_m01_t01_test")
        assert tracker.issues[link.linear_id]["state_id"] != linear.schema_cache.dispatch_to_state_id["COMPLETED"]


def test_reported_business_risk_does_not_reopen_a_completed_planning_task(setup_project):
    factory, linear, tracker, plan = setup_project
    dispatcher = CompletingDispatcher()
    with factory() as session:
        task = session.get(TaskModel, "RUN-MOD-01_po_define_backlog")
        dispatcher.dispatch_single_task(session, task)
        task.result = {**task.result, "outputs": [{"risks": [{"severity": "HIGH", "description": "Scope may change"}]}]}
        session.commit()
    assert drive(factory, ProjectCoordinator(factory, dispatcher, linear)).status == "COMPLETED"
    with factory() as session:
        from control.app.database.repositories import LinearLinkRepository
        link = LinearLinkRepository.get_link(session, "TASK", "RUN-MOD-01_po_define_backlog")
        assert tracker.issues[link.linear_id]["state_id"] == linear.schema_cache.dispatch_to_state_id["COMPLETED"]


def test_actual_dispatcher_honors_linear_barrier_and_includes_project_progress(setup_project):
    from agent_contract.system_models import DispatchAck
    from control.app.database.models import AgentModel, WorkerModel
    from control.app.services.dispatcher import DispatcherService
    factory, linear, tracker, plan = setup_project
    sent = []
    class Client:
        async def dispatch(self, request):
            sent.append(request)
            return DispatchAck(task_id=request.task_id, accepted=True)
    dispatcher = DispatcherService(factory, max_parallel_tasks=1)
    dispatcher.register_worker_client("worker-test", Client())
    with factory() as session:
        session.add(AgentModel(agent_id="po-01", role="PO", machine="PC3", runner="RemoteWorker", status="IDLE", capabilities=[]))
        session.add(WorkerModel(worker_id="worker-test", machine_id="PC3", base_url="http://worker-test", status="ONLINE",
            contract_version="1.3.0", worker_version="1.0.0"))
        session.commit()
        task = session.get(TaskModel, "RUN-MOD-01_po_define_backlog")
        assert dispatcher.dispatch_single_task(session, task) is False
        run = session.get(WorkflowRunModel, "RUN-MOD-01")
        run.run_metadata = {"linear_plan_ready": True}
        session.commit()
        assert dispatcher.dispatch_single_task(session, task) is True
    assert len(sent) == 1
    assert sent[0].context["execution_mode"] == "modules"
    assert sent[0].context["dispatch_token"] and sent[0].context["require_dispatch_token"]
    assert any(t["task_id"] == sent[0].task_id and t["status"] == "RUNNING" for t in sent[0].context["project_tasks"])


def test_api_creates_entire_module_backlog_before_background_execution(setup_project):
    from fastapi import BackgroundTasks
    from control.app.api.runs import CreateRunRequest, create_run, get_run_tasks
    from control.app.security.authentication import AdminPrincipal
    factory, linear, tracker, plan = setup_project
    with factory() as session:
        response = create_run(CreateRunRequest(run_id="RUN-API", brief="Build library", module_plan=proposal()),
            BackgroundTasks(), session, None, AdminPrincipal(actor_id="offline-test"))
        tasks = get_run_tasks(response["run_id"], session, None)
        assert len(tasks) == len(plan["tasks"])
        assert all(task["dispatch_state"] == "QUEUED" for task in tasks)
        assert {task["module_id"] for task in tasks if task["action"] == "implement_feature"} == {"books", "loans"}
