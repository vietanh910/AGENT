"""Immutable release snapshots and atomic promotion of a project's accepted version."""

from copy import deepcopy
import uuid

from sqlalchemy import select

from agent_contract.quality import quality_verdict
from control.app.database.models import ArtifactModel, ProjectModel, ProjectReleaseModel, TaskModel, WorkflowRunModel
from control.app.database.repositories import AuditRepository


class ReleaseError(ValueError):
    pass


def release_manifest(session, run, parent=None):
    previous = deepcopy(parent.manifest) if parent else {}
    tasks = {t.task_id: t for t in session.scalars(select(TaskModel).where(TaskModel.run_id == run.run_id))}
    if not tasks or any(t.dispatch_state != "COMPLETED" for t in tasks.values()):
        raise ReleaseError("Every delivery task must complete before creating a release")
    if not any(t.action == "accept_release" for t in tasks.values()):
        raise ReleaseError("Release acceptance is missing")
    for task in tasks.values():
        if task.action in {"run_test_suite", "security_review"}:
            verdict = quality_verdict(task.result)
            if verdict != "PASS" and not (task.context or {}).get("quality_repaired"):
                raise ReleaseError(f"Unresolved quality check: {task.task_id}")

    # Each slot retains all files from the latest producer of that deliverable.
    groups = {}
    artifacts = session.scalars(select(ArtifactModel).where(ArtifactModel.run_id == run.run_id)
        .order_by(ArtifactModel.created_at.desc(), ArtifactModel.artifact_id.desc())).all()
    for task in tasks.values():
        expected = (task.context or {}).get("output_artifact_type")
        if expected and not any(a.task_id == task.task_id and a.type == expected for a in artifacts):
            raise ReleaseError(f"Missing {expected} from completed task {task.task_id}")
    for artifact in artifacts:
        task = tasks.get(artifact.task_id)
        if not task:
            continue
        declared = (task.result or {}).get("artifacts")
        if isinstance(declared, list) and artifact.artifact_id not in {
                ref.get("artifact_id") for ref in declared if isinstance(ref, dict)}:
            continue
        expected = (task.context or {}).get("output_artifact_type")
        if expected and artifact.type != expected:
            continue
        module_id = (task.context or {}).get("module_id") if artifact.type != "SOURCE_CODE" else None
        slot = (artifact.type, module_id)
        group = groups.setdefault(slot, {"producer": task.task_id, "names": set(), "artifacts": []})
        if group["producer"] != task.task_id or artifact.name in group["names"]:
            continue
        group["names"].add(artifact.name)
        group["artifacts"].append({"artifact_id": artifact.artifact_id, "type": artifact.type,
            "module_id": module_id, "name": artifact.name, "uri": artifact.uri, "checksum": artifact.checksum,
            "version": artifact.version, "run_id": artifact.run_id, "task_id": artifact.task_id,
            "created_by": artifact.created_by})
    retained = [a for a in previous.get("artifacts", []) if (a["type"], a.get("module_id")) not in groups]
    retained.extend(a for group in groups.values() for a in group["artifacts"])
    for kind in {"SOURCE_CODE", "REQUIREMENT", "ARCHITECTURE", "DATABASE_SCHEMA", "USER_STORY", "BACKLOG", "TEST_CASE"}:
        if not any(a["type"] == kind for a in retained):
            raise ReleaseError(f"Release is missing {kind}")
    modules = {m["module_id"]: deepcopy(m) for m in previous.get("modules", [])}
    for module in (run.plan or {}).get("modules", []):
        old_tasks = {t["task_id"]: t for t in modules.get(module["module_id"], {}).get("tasks", [])}
        old_tasks.update({t["task_id"]: t for t in module["tasks"]})
        modules[module["module_id"]] = {**deepcopy(module), "tasks": list(old_tasks.values())}
    return {"project_name": previous.get("project_name") or (run.plan or {}).get("project_name", run.project_id),
        "project_id": run.project_id, "run_id": run.run_id, "modules": list(modules.values()),
        "artifacts": retained, "acceptance_task_ids": [t.task_id for t in tasks.values() if t.action == "accept_release"]}


def publish_release(session, run, artifact_store=None):
    """Caller commits run completion and the new current version in one transaction."""
    existing = session.scalar(select(ProjectReleaseModel).where(ProjectReleaseModel.run_id == run.run_id))
    if existing:
        return existing
    project = session.scalar(select(ProjectModel).where(ProjectModel.project_id == run.project_id)
                             .with_for_update().execution_options(populate_existing=True))
    # A second coordinator may have published while this transaction waited for the project lock.
    existing = session.scalar(select(ProjectReleaseModel).where(ProjectReleaseModel.run_id == run.run_id).with_for_update())
    if existing:
        return existing
    parent_id = (run.run_metadata or {}).get("base_release_id")
    if not project:
        if parent_id:
            raise ReleaseError("Change request has no project")
        project = ProjectModel(project_id=run.project_id, name=(run.plan or {}).get("project_name", run.project_id),
                               root_run_id=run.run_id, release_counter=0)
        session.add(project)
        session.flush()
    if project.current_release_id != parent_id:
        raise ReleaseError("The project's current release changed; rebase the change request before acceptance")
    if parent_id is None:
        project.root_run_id = run.run_id
    if parent_id and project.active_change_run_id != run.run_id:
        raise ReleaseError("This run no longer owns the project's change request")
    parent = session.get(ProjectReleaseModel, parent_id) if parent_id else None
    manifest = release_manifest(session, run, parent)
    from control.app.services.project_report import create_project_report, ProjectReportError, report_download_url
    from control.app.services.artifact_store import ArtifactStoreError
    version = project.release_counter + 1
    try:
        report = create_project_report(session, run, manifest, version, parent, artifact_store)
    except (ProjectReportError, ArtifactStoreError, OSError) as exc:
        raise ReleaseError(f"Final project report could not be created: {exc}") from exc
    report_ref = {key: getattr(report, key) for key in (
        "artifact_id", "type", "name", "uri", "checksum", "version", "run_id", "task_id", "created_by")}
    report_ref["module_id"] = None
    if not any(a["artifact_id"] == report.artifact_id for a in manifest["artifacts"]):
        manifest["artifacts"].append(report_ref)
    release = ProjectReleaseModel(release_id=f"REL-{uuid.uuid4().hex[:24].upper()}", project_id=run.project_id,
        run_id=run.run_id, version=version, base_release_id=parent_id, manifest=manifest)
    manifest["report"] = {**report_ref, "download_url": report_download_url(run.project_id, release.release_id)}
    session.add(release)
    project.current_release_id, project.release_counter = release.release_id, release.version
    project.active_change_run_id = None
    run.run_metadata = {**(run.run_metadata or {}), "release_id": release.release_id, "release_version": release.version,
                        "project_report": manifest["report"]}
    AuditRepository.record(session, actor="PM_COORDINATOR", action="RELEASE_ACCEPTED", target=release.release_id,
        run_id=run.run_id, result="SUCCESS", details={"project_id": run.project_id, "version": release.version,
                                                   "base_release_id": parent_id})
    session.flush()
    return release


def ensure_current_release(session, project_id, expected_run_id=None):
    project = session.scalar(select(ProjectModel).where(ProjectModel.project_id == project_id)
                             .with_for_update().execution_options(populate_existing=True))
    if not project or not project.current_release_id:
        # Adopt a completed pre-upgrade run without rerunning delivery or rewriting its existing artifacts.
        query = select(WorkflowRunModel).where(WorkflowRunModel.project_id == project_id,
                                              WorkflowRunModel.status == "COMPLETED")
        candidates = session.scalars(query.order_by(WorkflowRunModel.completed_at.desc(), WorkflowRunModel.created_at.desc())).all()
        base = next((r for r in candidates if (r.plan or {}).get("execution_mode") == "modules"), None)
        if not base:
            raise ReleaseError("No completed managed release exists for this project")
        release = publish_release(session, base)
        project = session.get(ProjectModel, project_id)
    else:
        release = session.scalar(select(ProjectReleaseModel).where(ProjectReleaseModel.release_id == project.current_release_id)
                                 .with_for_update())
    if not release or (expected_run_id and release.run_id != expected_run_id):
        raise ReleaseError("base_run_id is not the current accepted version of this project")
    return project, release


def pinned_baseline_artifacts(session, run, bindings):
    """Authorize cross-run reads only against this change's immutable accepted snapshot."""
    release_id = (run.run_metadata or {}).get("base_release_id") if run else None
    release = session.get(ProjectReleaseModel, release_id) if release_id else None
    if not release or release.project_id != run.project_id or (run.plan or {}).get("baseline_release_id", release_id) != release_id:
        raise ReleaseError("This run has no authorized baseline release")
    pinned = {a["artifact_id"]: a for a in release.manifest["artifacts"]}
    selected = []
    for kind, ids in bindings.items():
        for artifact_id in ids:
            record, artifact = pinned.get(artifact_id), session.get(ArtifactModel, artifact_id)
            if not record or not artifact or kind != artifact.type or not artifact.checksum:
                raise ReleaseError(f"Artifact {artifact_id} is not a pinned {kind} in the baseline")
            actual = {key: getattr(artifact, key) for key in ("checksum", "uri", "run_id", "task_id", "type", "version", "created_by", "name")}
            if any(record.get(key) != value for key, value in actual.items()):
                raise ReleaseError(f"Baseline artifact changed: {artifact_id}")
            selected.append(artifact)
    return selected
