"""Plan and execute a new change against the project's accepted release."""

import hashlib
import logging
import uuid

from sqlalchemy import func, select

from agent_contract.project_plan import ChangePlan, compile_change_plan
from control.app.config import settings
from control.app.database.models import ArtifactModel, ProjectReleaseModel, TaskModel, WorkflowRunModel
from control.app.database.repositories import AuditRepository, WorkflowRunRepository
from control.app.services.artifact_store import ArtifactStoreService, ArtifactStoreError
from control.app.services.plan_service import PlanService, build_default_plan
from control.app.services.plan_validator import PlanValidator
from control.app.services.project_releases import ensure_current_release, ReleaseError


class ChangeRequestError(ValueError):
    def __init__(self, message, status_code=409):
        super().__init__(message)
        self.status_code = status_code


class ChangeRequestService:
    def __init__(self, artifact_store=None, publisher=None):
        self.store = artifact_store or ArtifactStoreService()
        self.publisher = publisher

    def verify_baseline(self, session, release):
        """Fail before planning if the accepted files were removed or changed."""
        for entry in release.manifest["artifacts"]:
            artifact = session.get(ArtifactModel, entry["artifact_id"])
            if not artifact or not entry.get("checksum") or artifact.checksum != entry["checksum"]:
                raise ChangeRequestError(f"Baseline metadata/checksum is missing or changed: {entry['artifact_id']}", 422)
            if any(getattr(artifact, key) != entry.get(key) for key in ("uri", "name", "version", "type", "run_id", "task_id", "created_by")):
                raise ChangeRequestError(f"Baseline metadata changed: {entry['artifact_id']}", 422)
            try:
                path, _ = self.store.get_artifact_file_path(session, artifact.artifact_id)
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != entry["checksum"]:
                    raise ChangeRequestError(f"Baseline file checksum mismatch: {artifact.artifact_id}", 422)
            except ArtifactStoreError as exc:
                raise ChangeRequestError(str(exc), 422) from exc
            except OSError as exc:
                raise ChangeRequestError(f"Baseline file is not readable: {entry['artifact_id']}", 422) from exc

    def create(self, session, project_id, brief, actor, module_plan=None, base_run_id=None, title=None):
        brief = brief.strip()
        if not brief:
            raise ChangeRequestError("Describe the requested change", 422)
        try:
            project, base = ensure_current_release(session, project_id, base_run_id)
        except ReleaseError as exc:
            raise ChangeRequestError(str(exc)) from exc
        active = session.get(WorkflowRunModel, project.active_change_run_id) if project.active_change_run_id else None
        if active and active.status not in {"FAILED", "CANCELLED", "COMPLETED"}:
            raise ChangeRequestError(f"Project already has an active change: {active.run_id}")
        count = session.scalar(select(func.count()).select_from(WorkflowRunModel).where(
            WorkflowRunModel.status.in_(["PLANNING", "RUNNING", "AWAITING_APPROVAL", "CREATED"]))) or 0
        if count >= settings.MAX_CONCURRENT_RUNS:
            raise ChangeRequestError("MAX_CONCURRENT_RUNS reached", 429)
        self.verify_baseline(session, base)
        run_id = "CHG-" + uuid.uuid4().hex[:16].upper()
        proposal = ChangePlan.model_validate(module_plan).model_dump() if module_plan else None
        run = WorkflowRunRepository.create_run(session, {"run_id": run_id, "project_id": project_id,
            "brief": brief, "status": "PLANNING", "metadata": {
                "run_kind": "change", "base_release_id": base.release_id, "base_run_id": base.run_id,
                "linear_project_root_run_id": project.root_run_id, "target_version": base.version + 1,
                "change_request": {"brief": brief, "title": title or brief[:120], "requested_by": actor,
                                   "module_plan": proposal}}})
        project.active_change_run_id = run_id
        AuditRepository.record(session, actor=actor, action="CHANGE_REQUESTED", target=run_id, run_id=run_id,
            result="PLANNING", details={"project_id": project_id, "base_release_id": base.release_id,
                                       "base_run_id": base.run_id, "brief": brief})
        # A supplied plan is cheap to compile; commit only after it passes all validation.
        if proposal:
            self.plan(session, run_id)
        else:
            session.commit()
        return run

    def _planning_context(self, session, base):
        documents, remaining = [], 100000
        entries = sorted(base.manifest["artifacts"], key=lambda a: (a.get("module_id") is not None,
            a["type"] not in {"REQUIREMENT", "ARCHITECTURE"}))
        for entry in entries:
            if entry["type"] not in {"REQUIREMENT", "ARCHITECTURE", "DATABASE_SCHEMA", "USER_STORY", "BACKLOG"}:
                continue
            if remaining <= 0:
                break
            path, _ = self.store.get_artifact_file_path(session, entry["artifact_id"])
            with path.open("rb") as stream:
                content = stream.read(min(16000, remaining)).decode("utf-8", errors="replace")
            remaining -= len(content)
            documents.append({"type": entry["type"], "module_id": entry.get("module_id"), "content": content})
        return {"change_request": True, "baseline_release_id": base.release_id,
            "baseline_version": base.version, "baseline_modules": base.manifest.get("modules", []),
            "baseline_documents": documents, "project_name": base.manifest["project_name"]}

    def plan(self, session, run_id, pm_caller=None):
        run = session.scalar(select(WorkflowRunModel).where(WorkflowRunModel.run_id == run_id)
                             .with_for_update().execution_options(populate_existing=True))
        if not run or (run.run_metadata or {}).get("run_kind") != "change":
            raise ChangeRequestError("Unknown change request", 404)
        if session.scalar(select(TaskModel.task_id).where(TaskModel.run_id == run_id).limit(1)):
            return run.plan
        if run.status != "PLANNING":
            raise ChangeRequestError("Change request is no longer planning")
        base = session.get(ProjectReleaseModel, run.run_metadata["base_release_id"])
        self.verify_baseline(session, base)
        context = self._planning_context(session, base)
        supplied = run.run_metadata["change_request"].get("module_plan")
        planner = PlanService(publisher=self.publisher)
        caller = pm_caller or planner._call_pm_proposal
        errors, selected = None, None
        for attempt in range(1 if supplied else 3):
            try:
                output = {"module_plan": supplied} if supplied else caller(run.brief, run.project_id, errors, context)
                candidate = ChangePlan.model_validate(output["module_plan"])
                candidate.project_name = base.manifest["project_name"]
                candidate.overview = run.brief
                selected = compile_change_plan(build_default_plan(run.brief, run.project_id), candidate.model_dump(),
                                               base.manifest, base.release_id)
                validation = PlanValidator().validate(selected)
                if not validation.valid:
                    raise ValueError("; ".join(validation.errors))
                if output.get("planning_warning"):
                    selected["planning_warning"] = output["planning_warning"]
                break
            except (ValueError, RuntimeError) as exc:
                if supplied:
                    raise ChangeRequestError(str(exc), 422) from exc
                errors, selected = [str(exc)], None
        if selected is None:
            fallback = {"project_name": base.manifest["project_name"], "overview": run.brief,
                "impact_summary": "Planning failed; review affected modules before approval",
                "architecture_changed": True, "database_changed": True,
                "modules": [{"module_id": "change", "name": "Requested change", "description": run.brief,
                    "tasks": [{"task_id": "change", "title": "Implement requested change", "description": run.brief,
                        "acceptance_criteria": ["Requested behavior works and existing behavior is preserved"]}]}]}
            selected = compile_change_plan(build_default_plan(run.brief, run.project_id), fallback, base.manifest, base.release_id)
            selected["planning_warning"] = "PM change planning failed; review the fallback impact and scope before approval."
        selected["change_title"] = run.run_metadata["change_request"]["title"]
        selected["epics"][0]["name"] = f"v{base.version + 1} - {selected['change_title']}"
        planner.save_plan_to_db(session, run_id, selected)
        return selected


def plan_change_in_background(run_id):
    """Safe to retry after a restart: the run row serializes planning and task creation."""
    from control.app.database.session import get_session_factory
    from control.app.database.models import ProjectModel
    with get_session_factory()() as session:
        try:
            ChangeRequestService().plan(session, run_id)
        except Exception:
            session.rollback()
            logging.getLogger(__name__).exception("Change planning failed for %s; accepted release is unchanged", run_id)
            run = session.get(WorkflowRunModel, run_id)
            if run and run.status == "PLANNING":
                run.status = "FAILED"
                project = session.get(ProjectModel, run.project_id)
                if project and project.active_change_run_id == run_id:
                    project.active_change_run_id = None
                session.commit()
