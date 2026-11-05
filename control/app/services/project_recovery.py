"""Durable PM recovery: restore evidence, replay invalidated work, then execute locally."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from sqlalchemy import select

from agent_contract.models import TaskRequest
from control.app.config import settings
from control.app.database.models import AgentModel, ArtifactModel, ProjectReleaseModel, TaskDependencyModel, TaskModel, utc_now
from control.app.database.repositories import AuditRepository
from control.app.services.artifact_store import ArtifactStoreService, model_to_artifact_ref, sanitize_filename, select_input_artifacts


RECOVERABLE_ACTIONS = {
    "define_backlog", "write_requirements", "write_user_stories", "design_architecture", "design_schema",
    "implement_project", "implement_feature", "fix_bugs", "create_test_cases",
    "run_sprint_review", "run_retrospective", "track_blockers", "report_status",
}


def due(value):
    if not value:
        return True
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc) <= utc_now()


class ProjectRecoveryService:
    def __init__(self, store=None, pm_factory=None):
        self.store = store or ArtifactStoreService()
        self.pm_factory = pm_factory

    def waiting(self, run):
        return not due(((run.run_metadata or {}).get("recovery") or {}).get("next_retry_at"))

    def wait(self, session, run, reason, task=None):
        session.flush()
        session.refresh(run, with_for_update=True)
        if run.status not in {"RUNNING", "BLOCKED"}:
            return
        previous = (run.run_metadata or {}).get("recovery") or {}
        count = previous.get("count", 0) + 1
        delay = min(settings.PM_RECOVERY_MAX_DELAY_SECONDS,
                    settings.PM_RECOVERY_DELAY_SECONDS * 2 ** min(count - 1, 8))
        state = {"reason": str(reason), "task_id": task.task_id if task else None, "count": count,
                 "next_retry_at": (utc_now() + timedelta(seconds=delay)).isoformat()}
        run.status, run.current_stage = "RUNNING", "pm_recovery"
        run.run_metadata = {**(run.run_metadata or {}), "recovery": state}
        AuditRepository.record(session, actor="PM_COORDINATOR", action="PROJECT_AUTO_RECOVERY",
            target=task.task_id if task else run.run_id, run_id=run.run_id, result="RETRY_WAIT", details=state)
        session.commit()

    def restore(self, session, artifact):
        """Restore only the exact bytes identified by the immutable accepted checksum."""
        metadata = artifact.metadata_json or {}
        path = Path(metadata["file_path"]) if metadata.get("file_path") else None
        if path and path.is_file() and (not artifact.checksum or sha256(path.read_bytes()).hexdigest() == artifact.checksum):
            return True
        candidates = [self.store.base_dir / "_recovery" / (artifact.checksum or "missing")]
        if metadata.get("recovery_copy"):
            candidates.insert(0, Path(metadata["recovery_copy"]))
        for backup in candidates:
            if not artifact.checksum or not backup.is_file():
                continue
            content = backup.read_bytes()
            if sha256(content).hexdigest() != artifact.checksum:
                continue
            target = (self.store.base_dir / artifact.run_id / f"{artifact.artifact_id}-{sanitize_filename(artifact.name)}").resolve()
            if not target.is_relative_to(self.store.base_dir.resolve()):
                raise ValueError("Recovery target is outside the artifact store")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            artifact.metadata_json = {**metadata, "file_path": str(target)}
            AuditRepository.record(session, actor="PM_COORDINATOR", action="ARTIFACT_RESTORED",
                target=artifact.artifact_id, run_id=artifact.run_id, result="SUCCESS",
                details={"checksum": artifact.checksum})
            return True
        return False

    def reconcile(self, session, task):
        from agent_contract.quality import quality_verdict
        if task.action in {"run_test_suite", "security_review"} and quality_verdict(task.result) is None:
            return False
        expected = (task.context or {}).get("output_artifact_type")
        if not expected:
            return False
        rows = list(session.scalars(select(ArtifactModel).where(ArtifactModel.run_id == task.run_id,
            ArtifactModel.task_id == task.task_id, ArtifactModel.created_by == task.agent_id)
            .order_by(ArtifactModel.created_at.desc())))
        declared = (task.result or {}).get("artifacts") or []
        ids = {ref.get("artifact_id") for ref in declared if isinstance(ref, dict)}
        if ids and ids <= {a.artifact_id for a in rows}:
            rows = [a for a in rows if a.artifact_id in ids]
        else:
            rows = [a for a in rows if a.type == expected]
        # Do not invent quality outcomes or replace lost evidence with a guessed document.
        if not any(a.type == expected for a in rows) or not all(self.restore(session, a) for a in rows):
            return False
        result = dict(task.result or {})
        result["artifacts"] = [model_to_artifact_ref(a).model_dump(mode="json") for a in rows]
        task.result = result
        session.flush()
        return True

    def retry(self, session, run, task, reason):
        """Invalidate descendants, so regenerated source cannot inherit stale test success."""
        if run.status != "RUNNING" or task.run_id != run.run_id or session.scalar(
                select(ProjectReleaseModel.release_id).where(ProjectReleaseModel.run_id == run.run_id)):
            raise ValueError("Recovery cannot rewrite an accepted release or a stopped run")
        all_tasks = {t.task_id: t for t in session.scalars(select(TaskModel).where(TaskModel.run_id == run.run_id))}
        edges = session.execute(select(TaskDependencyModel.task_id, TaskDependencyModel.depends_on_task_id)
            .where(TaskDependencyModel.task_id.in_(all_tasks))).all()
        affected = {task.task_id}
        while True:
            expanded = affected | {child for child, parent in edges if parent in affected}
            if expanded == affected:
                break
            affected = expanded
        for task_id in affected:
            item = all_tasks[task_id]
            if item is not task and item.dispatch_state not in {"COMPLETED", "BLOCKED", "FAILED", "RETRY_WAIT", "QUEUED"}:
                raise ValueError("A descendant is still executing; wait for its deadline before recovery")
        count = int((task.context or {}).get("pm_recovery_count", 0)) + 1
        for task_id in affected:
            item = all_tasks[task_id]
            agent = session.get(AgentModel, item.agent_id)
            if agent and agent.current_task == item.task_id:
                agent.current_task = None
                if agent.status != "OFFLINE":
                    agent.status = "IDLE"
            item.context = {k: v for k, v in (item.context or {}).items()
                            if k not in {"quality_handled", "quality_repaired", "completion_error", "dispatch_stalled_since"}}
            item.context = {**item.context, "require_dispatch_token": True, "dispatch_token": uuid.uuid4().hex}
            item.dispatch_state, item.result, item.error, item.deadline = "QUEUED", None, None, None
            item.updated_at, item.version = utc_now(), item.version + 1
            item.max_attempts = max(item.max_attempts, item.attempts + 1)
        original = (task.context or {}).get("recovery_original") or {
            "role": task.role, "action": task.action, "agent_id": task.agent_id, "instruction": task.instruction}
        task.context = {**(task.context or {}), "pm_recovery_count": count, "recovery_original": original,
                        "recovery_reason": str(reason), "require_dispatch_token": True}
        if count > settings.PM_RECOVERY_RETRIES and original["action"] in RECOVERABLE_ACTIONS:
            task.role, task.agent_id, task.action = "PM", "pm-01", "recover_task"
            task.context = {**task.context, "pm_takeover": True}
        AuditRepository.record(session, actor="PM_COORDINATOR", action="TASK_RECOVERY_SCHEDULED",
            target=task.task_id, run_id=run.run_id, result="PM_TAKEOVER" if task.context.get("pm_takeover") else "RETRY",
            details={"reason": str(reason), "count": count, "invalidated_tasks": sorted(affected), "original": original})
        self.wait(session, run, reason, task)

    def recover_task(self, session, run, task, reason):
        if task.dispatch_state == "COMPLETED" and self.reconcile(session, task):
            session.commit()
            return
        # Repair bound inputs before retrying the consumer. Baselines are restored byte-for-byte only.
        bindings = (task.context or {}).get("input_artifact_sources", {})
        for kind, producer_ids in bindings.items():
            for producer_id in producer_ids:
                producer = session.get(TaskModel, producer_id)
                if not producer or producer.run_id != run.run_id:
                    self.wait(session, run, f"Missing producer {producer_id}: {reason}", task)
                    return
                if producer.dispatch_state != "COMPLETED":
                    self.retry(session, run, producer, reason)
                    return
                refs = (producer.result or {}).get("artifacts") or []
                rows = [session.get(ArtifactModel, ref.get("artifact_id")) for ref in refs if isinstance(ref, dict) and ref.get("type") == kind]
                if not rows or any(a is None or not self.restore(session, a) for a in rows):
                    self.retry(session, run, producer, reason)
                    return
        for ids in (task.context or {}).get("baseline_artifact_ids", {}).values():
            for artifact_id in ids:
                artifact = session.get(ArtifactModel, artifact_id)
                if not artifact or not self.restore(session, artifact):
                    self.wait(session, run, f"Accepted baseline unavailable: {artifact_id}", task)
                    return
        self.retry(session, run, task, reason)

    def execute_pm(self, session, run, task):
        """Run PM locally, independently of the failed remote agent/worker."""
        with tempfile.TemporaryDirectory(prefix="pm-recovery-") as temporary:
            root = Path(temporary)
            artifact_dir = root / "artifacts"
            inbox = artifact_dir / "inbox"
            inbox.mkdir(parents=True)
            refs = select_input_artifacts(session, task)
            for ref in refs:
                artifact = session.get(ArtifactModel, ref.artifact_id)
                if not self.restore(session, artifact):
                    raise ValueError(f"Recovery input file unavailable: {ref.artifact_id}")
                path = Path(artifact.metadata_json["file_path"])
                filename = f"{ref.artifact_id}-{sanitize_filename(ref.name)}"
                (inbox / filename).write_bytes(path.read_bytes())
                ref.uri = f"artifacts/inbox/{filename}"
            if self.pm_factory:
                pm = self.pm_factory(artifact_dir)
            else:
                from worker.host.process_agent import ProcessAgent
                environment = {"WORKSPACE_DIR": str(root / "workspace"), "DATA_DIR": str(root / "data")}
                if settings.LLM_API_KEY and "placeholder" not in settings.LLM_API_KEY.lower():
                    environment.update({"LLM_PROVIDER": settings.LLM_PROVIDER, "LLM_MODEL": settings.LLM_MODEL,
                        "LLM_API_KEY": settings.LLM_API_KEY, "LLM_BASE_URL": settings.LLM_BASE_URL})
                pm = ProcessAgent(SimpleNamespace(agent_id="pm-01", role="PM",
                    path=str(Path(__file__).resolve().parents[3] / "agents" / "pm")), environment)
                pm.config.artifact_dir = str(artifact_dir)
            task.context = {**(task.context or {}), "dispatch_token": uuid.uuid4().hex}
            session.refresh(run, with_for_update=True)
            if run.status != "RUNNING":
                session.rollback()
                return
            task.dispatch_state, task.deadline = "RUNNING", utc_now() + timedelta(seconds=task.timeout_seconds)
            task.attempts += 1
            session.commit()
            request = TaskRequest(task_id=task.task_id, project_id=run.run_id, role="PM", agent_id="pm-01",
                action="recover_task", instruction=task.instruction, context=deepcopy(task.context), input_artifacts=refs,
                constraints={"timeout_seconds": task.timeout_seconds})
            result = pm.handle(request)
            token = request.context["dispatch_token"]
            session.refresh(run, with_for_update=True)
            session.refresh(task, with_for_update=True)
            if run.status != "RUNNING" or (task.context or {}).get("dispatch_token") != token:
                session.rollback()
                return
            if result.status.value != "COMPLETED":
                raise ValueError(result.error.message if result.error else result.summary)
            uploaded = []
            for ref in result.artifacts:
                relative = ref.uri.replace("\\", "/")
                path = (root / relative).resolve()
                if not path.is_relative_to(artifact_dir.resolve()) or not path.is_file():
                    raise ValueError("PM recovery did not produce a local deliverable")
                content = path.read_bytes()
                if ref.checksum and sha256(content).hexdigest() != ref.checksum:
                    raise ValueError("PM recovery output checksum mismatch")
                model = self.store.save_artifact(session, content, ref.name, task.run_id, task.task_id,
                    ref.type, "pm-01", metadata={"recovered_by_pm": True})
                uploaded.append(model_to_artifact_ref(model))
            if not any(ref.type.value == task.context.get("output_artifact_type") for ref in uploaded):
                raise ValueError("PM recovery returned no required artifact")
            result.artifacts = uploaded
            task.result, task.error, task.dispatch_state, task.deadline = result.model_dump(mode="json"), None, "COMPLETED", None
            task.context = {**task.context, "pm_takeover": False}
            AuditRepository.record(session, actor="pm-01", action="PM_RECOVERY_COMPLETED", target=task.task_id,
                run_id=run.run_id, result="SUCCESS", details={"artifact_ids": [ref.artifact_id for ref in uploaded]})
            session.commit()
