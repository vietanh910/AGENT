"""Recovery scenarios use real files, persistent SQL state and the normal delivery coordinator."""

from copy import deepcopy
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_contract.models import ArtifactRef, TaskResult
from control.app.config import settings
from control.app.database.models import ArtifactModel, ProjectModel, TaskModel, WorkflowRunModel, utc_now
from control.app.services.project_coordinator import ProjectCoordinator
from control.app.services.project_recovery import ProjectRecoveryService
from control.tests.test_change_requests import project, request, drive, change_plan


@pytest.fixture
def recovery_project(project, monkeypatch):
    monkeypatch.setattr(settings, "PM_AUTO_RECOVERY", True)
    monkeypatch.setattr(settings, "PM_RECOVERY_DELAY_SECONDS", 0)
    monkeypatch.setattr(settings, "PM_RECOVERY_RETRIES", 1)
    monkeypatch.setattr(settings, "PM_STALL_SECONDS", 0)
    project["pm_calls"] = []

    def pm_factory(artifact_dir):
        def handle(payload):
            project["pm_calls"].append(payload)
            path = artifact_dir / "recovered.md"
            path.write_text("# SRS recovered by PM\nRenew a loan at most twice.", encoding="utf-8")
            return TaskResult(task_id=payload.task_id, agent_id="pm-01", status="COMPLETED", summary="Recreated missing SRS",
                artifacts=[ArtifactRef(artifact_id="ART-LOCAL", name=path.name,
                    uri="artifacts/" + path.name, type=payload.context["output_artifact_type"],
                    checksum=sha256(path.read_bytes()).hexdigest(), created_by="pm-01", created_at=utc_now())])
        return SimpleNamespace(handle=handle)

    project["coordinator"].recovery.pm_factory = pm_factory
    return project


def test_lost_baseline_file_is_restored_exactly_and_delivery_continues(recovery_project):
    env = recovery_project
    change = request(env, change_plan())
    with env["factory"]() as session:
        original = session.scalar(select(ArtifactModel).where(ArtifactModel.run_id == "RUN-BASE", ArtifactModel.type == "ARCHITECTURE"))
        old_id, checksum = original.artifact_id, original.checksum
        path = Path(original.metadata_json["file_path"])
        before = path.read_bytes()
        path.unlink()
    result = drive(env, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    with env["factory"]() as session:
        restored = session.get(ArtifactModel, old_id)
        assert Path(restored.metadata_json["file_path"]).read_bytes() == before
        assert restored.checksum == checksum
    assert not env["pm_calls"]


def test_missing_output_reference_is_rebuilt_from_stored_file_without_rerunning_agent(recovery_project):
    env = recovery_project
    real = env["dispatcher"].dispatch_single_task
    broken = []

    def missing_ref(session, task):
        result = real(session, task)
        if task.action == "write_requirements" and not broken:
            broken.append(task.task_id)
            task.result = {**task.result, "artifacts": []}
            session.commit()
        return result

    env["dispatcher"].dispatch_single_task = missing_ref
    change = request(env, change_plan())
    assert drive(env, change.run_id).status == "COMPLETED"
    with env["factory"]() as session:
        task = session.get(TaskModel, broken[0])
        assert task.result["artifacts"]
        assert not task.context.get("pm_recovery_count")
    assert not env["pm_calls"]


@pytest.mark.parametrize("failure", ["missing_output", "offline", "blocked"])
def test_pm_takes_over_missing_documents_after_retries_and_survives_restart(recovery_project, failure):
    env = recovery_project
    real = env["dispatcher"].dispatch_single_task
    broken = []

    def fail(session, task):
        if task.action == "write_requirements":
            broken.append(task.task_id)
            if failure == "offline":
                return False
            task.dispatch_state = "COMPLETED" if failure == "missing_output" else "BLOCKED"
            task.result = {"status": task.dispatch_state, "artifacts": []}
            task.error = {"code": "TOOL_ERROR", "message": "Remote agent unavailable"}
            session.commit()
            return True
        return real(session, task)

    env["dispatcher"].dispatch_single_task = fail
    change = request(env, change_plan())
    for _ in range(30):
        env["coordinator"].tick(change.run_id)
        with env["factory"]() as session:
            task = next((t for t in session.scalars(select(TaskModel).where(TaskModel.run_id == change.run_id))
                         if (t.context or {}).get("pm_takeover")), None)
            if task:
                break
    else:
        pytest.fail("PM takeover was not scheduled")
    recovery = ProjectRecoveryService(env["store"], env["coordinator"].recovery.pm_factory)
    env["coordinator"] = ProjectCoordinator(env["factory"], env["dispatcher"], env["linear"], env["store"], recovery)
    result = drive(env, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    assert env["pm_calls"]
    with env["factory"]() as session:
        recovered = session.get(TaskModel, env["pm_calls"][0].task_id)
        assert recovered.role == "PM" and recovered.action == "recover_task"
        assert recovered.context["recovery_original"]["role"] == "BA"
        assert recovered.result["artifacts"][0]["created_by"] == "pm-01"
        assert session.get(ProjectModel, "LIB").release_counter == 2


def test_expired_execution_is_retried_without_manual_resume(recovery_project):
    env = recovery_project
    real = env["dispatcher"].dispatch_single_task
    expired = []

    def expire_once(session, task):
        if task.action == "write_requirements" and not expired:
            expired.append(task.task_id)
            task.dispatch_state, task.deadline = "RUNNING", utc_now() - timedelta(seconds=1)
            session.commit()
            return True
        return real(session, task)

    env["dispatcher"].dispatch_single_task = expire_once
    change = request(env, change_plan())
    assert drive(env, change.run_id).status == "COMPLETED"
    with env["factory"]() as session:
        assert session.get(TaskModel, expired[0]).context["pm_recovery_count"] == 1


def test_source_regeneration_invalidates_passed_tests_and_keeps_old_artifacts(recovery_project):
    env = recovery_project
    change = request(env, change_plan())
    for _ in range(40):
        env["coordinator"].tick(change.run_id)
        with env["factory"]() as session:
            run = session.get(WorkflowRunModel, change.run_id)
            if run.status == "AWAITING_APPROVAL":
                from control.app.services.approval import ApprovalService
                from control.app.services.task_manager import TaskManager
                gate = next((t.action for t in TaskManager.get_ready_tasks(session, change.run_id) if t.role == "GATE"), "APPROVE_PLAN")
                ApprovalService().approve(session, change.run_id, gate=gate, decided_by="offline-test")
            test = session.scalar(select(TaskModel).where(TaskModel.run_id == change.run_id,
                TaskModel.action == "run_test_suite", TaskModel.dispatch_state == "COMPLETED"))
            if test:
                old_test_id, old_result = test.task_id, deepcopy(test.result)
                source_id = test.context["input_artifact_sources"]["SOURCE_CODE"][0]
                run = session.get(WorkflowRunModel, change.run_id)
                source = session.get(TaskModel, source_id)
                env["coordinator"].recovery.retry(session, run, source, "Recreate missing cumulative source")
                break
    else:
        pytest.fail("No test completed")
    with env["factory"]() as session:
        test = session.get(TaskModel, old_test_id)
        assert test.dispatch_state == "QUEUED" and test.result is None
        assert not test.context.get("quality_handled")
        assert session.get(ProjectModel, "LIB").release_counter == 1
    result = drive(env, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    with env["factory"]() as session:
        current = session.get(TaskModel, old_test_id)
        assert current.result["artifacts"][0]["artifact_id"] != old_result["artifacts"][0]["artifact_id"]
        old = session.get(ArtifactModel, old_result["artifacts"][0]["artifact_id"])
        assert Path(old.metadata_json["file_path"]).is_file()


def test_unavailable_pm_keeps_retry_state_and_never_fakes_completion(recovery_project):
    env = recovery_project
    real = env["dispatcher"].dispatch_single_task

    def unavailable(session, task):
        return False if task.action == "write_requirements" else real(session, task)

    def unavailable_pm(artifact_dir):
        raise ConnectionError("PM LLM is unavailable")

    env["dispatcher"].dispatch_single_task = unavailable
    env["coordinator"].recovery.pm_factory = unavailable_pm
    change = request(env, change_plan())
    for _ in range(30):
        env["coordinator"].tick(change.run_id)
    with env["factory"]() as session:
        run = session.get(WorkflowRunModel, change.run_id)
        assert run.status == "RUNNING" and "PM takeover" in run.run_metadata["recovery"]["reason"]
        assert session.get(ProjectModel, "LIB").release_counter == 1
        assert not run.run_metadata.get("project_report")


def test_no_recovery_overwrites_accepted_release_or_approves_gate(recovery_project):
    env = recovery_project
    with env["factory"]() as session:
        baseline = session.get(WorkflowRunModel, "RUN-BASE")
        task = session.scalar(select(TaskModel).where(TaskModel.run_id == "RUN-BASE", TaskModel.action == "implement_feature"))
        with pytest.raises(ValueError, match="accepted release"):
            env["coordinator"].recovery.retry(session, baseline, task, "Must not overwrite baseline")
    change = request(env, change_plan())
    for _ in range(25):
        env["coordinator"].tick(change.run_id)
        with env["factory"]() as session:
            run = session.get(WorkflowRunModel, change.run_id)
            if run.status == "AWAITING_APPROVAL":
                break
    else:
        pytest.fail("Approval was bypassed")
    assert run.status == "AWAITING_APPROVAL"


def test_stale_callback_cannot_complete_a_new_recovery_attempt(recovery_project):
    from control.app.security.authentication import WorkerPrincipal
    from control.app.services.result_handler import ResultHandler
    env = recovery_project
    change = request(env, change_plan())
    with env["factory"]() as session:
        task = session.scalar(select(TaskModel).where(TaskModel.run_id == change.run_id, TaskModel.action == "write_requirements"))
        task.dispatch_state = "RUNNING"
        task.context = {**task.context, "require_dispatch_token": True, "dispatch_token": "new-attempt"}
        session.commit()
        result = TaskResult(task_id=task.task_id, agent_id=task.agent_id, status="COMPLETED",
            summary="Late callback from old execution", metadata={"dispatch_token": "old-attempt"})
        worker = WorkerPrincipal(worker_id="PC3", machine_id="PC3", allowed_agents=[task.agent_id])
        response = ResultHandler.handle_result(session, task.task_id, result, worker)
        assert response["status"] == "ignored_stale_attempt"
        assert task.dispatch_state == "RUNNING" and task.result is None
        original_agent = task.agent_id
        task.context = {**task.context, "recovery_original": {"agent_id": original_agent, "role": task.role, "action": task.action}}
        task.agent_id, task.role, task.action = "pm-01", "PM", "recover_task"
        response = ResultHandler.handle_result(session, task.task_id, result, worker)
        assert response["status"] == "ignored_stale_attempt"
        assert task.agent_id == "pm-01" and task.result is None


def test_cancelled_project_is_not_resumed_when_pm_takeover_fails(recovery_project):
    env = recovery_project
    real = env["dispatcher"].dispatch_single_task
    env["dispatcher"].dispatch_single_task = lambda session, task: False if task.action == "write_requirements" else real(session, task)
    change = request(env, change_plan())

    def cancel_then_fail(artifact_dir):
        with env["factory"]() as session:
            session.get(WorkflowRunModel, change.run_id).status = "CANCELLED"
            session.commit()
        raise ConnectionError("LLM disconnected after user cancellation")

    env["coordinator"].recovery.pm_factory = cancel_then_fail
    result = drive(env, change.run_id)
    assert result.status == "CANCELLED"
    with env["factory"]() as session:
        assert session.get(ProjectModel, "LIB").release_counter == 1


def test_fast_recovery_callback_before_dispatch_ack_is_not_lost(recovery_project):
    from agent_contract.system_models import DispatchAck
    from control.app.database.models import AgentModel, WorkerModel
    from control.app.security.authentication import WorkerPrincipal
    from control.app.services.dispatcher import DispatcherService
    from control.app.services.result_handler import ResultHandler
    env = recovery_project
    change = request(env, change_plan())

    class Client:
        async def dispatch(self, payload):
            result = TaskResult(task_id=payload.task_id, agent_id=payload.agent_id, status="COMPLETED",
                summary="Fast callback", metadata={"dispatch_token": payload.context["dispatch_token"]})
            with env["factory"]() as callback_session:
                response = ResultHandler.handle_result(callback_session, payload.task_id, result,
                    WorkerPrincipal(worker_id="recovery-worker", machine_id="PC3", allowed_agents=[payload.agent_id]))
                assert response["status"] == "accepted"
            return DispatchAck(task_id=payload.task_id, accepted=True)

    dispatcher = DispatcherService(env["factory"], max_parallel_tasks=1)
    dispatcher.register_worker_client("recovery-worker", Client())
    with env["factory"]() as session:
        session.add(AgentModel(agent_id="po-01", role="PO", machine="PC3", runner="RemoteWorker", status="IDLE", capabilities=[]))
        session.add(WorkerModel(worker_id="recovery-worker", machine_id="PC3", base_url="http://recovery-worker", status="ONLINE",
            contract_version="1.4.0", worker_version="1.0.0"))
        run = session.get(WorkflowRunModel, change.run_id)
        run.run_metadata = {**run.run_metadata, "linear_plan_ready": True}
        task = session.scalar(select(TaskModel).where(TaskModel.run_id == change.run_id, TaskModel.action == "define_backlog"))
        task.context = {**task.context, "require_dispatch_token": True}
        session.commit()
        assert dispatcher.dispatch_single_task(session, task)
        session.refresh(task)
        assert task.dispatch_state == "COMPLETED"
        assert task.result["summary"] == "Fast callback"


def test_upload_retry_can_adopt_identical_file_left_by_a_database_rollback(recovery_project):
    env = recovery_project
    change = request(env, change_plan())
    data, artifact_id, name = b"Existing exact bytes from interrupted upload", "ART-INTERRUPTED", "doc.md"
    directory = env["store"].base_dir / change.run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{artifact_id}-{name}").write_bytes(data)
    with env["factory"]() as session:
        task = session.scalar(select(TaskModel).where(TaskModel.run_id == change.run_id, TaskModel.action == "write_requirements"))
        artifact = env["store"].save_artifact(session, data, name, change.run_id, task.task_id,
            "REQUIREMENT", task.agent_id, artifact_id=artifact_id)
        session.commit()
        assert artifact.checksum == sha256(data).hexdigest()
        assert Path(artifact.metadata_json["recovery_copy"]).read_bytes() == data


@pytest.mark.parametrize("limit", ["fix_loops", "task_count"])
def test_quality_repair_limit_reuses_implementation_and_retests_instead_of_stopping(recovery_project, monkeypatch, limit):
    env = recovery_project
    env["dispatcher"].failures = 4 if limit == "fix_loops" else 1
    change = request(env, change_plan())
    if limit == "task_count":
        with env["factory"]() as session:
            task_count = len(session.scalars(select(TaskModel).where(TaskModel.run_id == change.run_id)).all())
        monkeypatch.setattr(settings, "MAX_TASKS_PER_RUN", task_count)
    result = drive(env, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    expected_fixes = settings.MAX_FIX_LOOPS if limit == "fix_loops" else 0
    assert result.run_metadata.get("fix_loop_count", 0) == expected_fixes
    with env["factory"]() as session:
        fixes = session.scalars(select(TaskModel).where(TaskModel.run_id == change.run_id, TaskModel.action == "fix_bugs")).all()
        assert len(fixes) == expected_fixes
        tasks = session.scalars(select(TaskModel).where(TaskModel.run_id == change.run_id)).all()
        assert any(t.context.get("pm_recovery_count") for t in tasks)


def test_missing_quality_verdict_is_reexecuted_without_pm_fabricating_pass(recovery_project):
    env = recovery_project
    real = env["dispatcher"].dispatch_single_task
    failures = []

    def missing_verdict(session, task):
        result = real(session, task)
        if task.action == "run_test_suite" and len(failures) < 2:
            failures.append(task.task_id)
            task.result = {**task.result, "outputs": []}
            session.commit()
        return result

    env["dispatcher"].dispatch_single_task = missing_verdict
    change = request(env, change_plan())
    result = drive(env, change.run_id)
    assert result.status == "COMPLETED", result.run_metadata
    with env["factory"]() as session:
        task = session.get(TaskModel, failures[0])
        assert task.role == "TESTER" and task.action == "run_test_suite"
        assert task.context["pm_recovery_count"] == 2
        assert task.result["outputs"][0]["verdict"] == "PASS"
    assert not env["pm_calls"]
