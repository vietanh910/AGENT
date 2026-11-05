"""Stable project identities, accepted versions and incremental change requests."""

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from agent_contract.project_plan import ChangePlan
from control.app.api.dependencies import get_db, get_publisher, require_admin, require_authenticated
from control.app.database.models import ArtifactModel, ProjectModel, ProjectReleaseModel, WorkflowRunModel
from control.app.security.authentication import AdminPrincipal, Principal
from control.app.services.change_requests import ChangeRequestService, ChangeRequestError
from control.app.services.artifact_store import ArtifactStoreService
from control.app.services.project_report import artifact_path, DOCX_MEDIA_TYPE
from control.app.database.repositories import AuditRepository

router = APIRouter(prefix="/projects", tags=["Projects and changes"])


class CreateChangeRequest(BaseModel):
    brief: str = Field(min_length=1)
    title: str | None = Field(None, max_length=255)
    base_run_id: str | None = Field(None, max_length=64)
    module_plan: ChangePlan | None = None


class RenameProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


def project_summary(project):
    return {"project_id": project.project_id, "name": project.name, "root_run_id": project.root_run_id,
        "current_release_id": project.current_release_id, "current_version": project.release_counter,
        "active_change_run_id": project.active_change_run_id}


def release_summary(release, include_manifest=False):
    data = {"release_id": release.release_id, "project_id": release.project_id, "version": release.version,
        "run_id": release.run_id, "base_release_id": release.base_release_id,
        "created_at": release.created_at.isoformat()}
    data["report"] = (release.manifest or {}).get("report")
    if include_manifest:
        data["manifest"] = release.manifest
    return data


def _background_change_plan(run_id):
    from control.app.services.change_requests import plan_change_in_background
    plan_change_in_background(run_id)


@router.get("")
def list_projects(session: Session = Depends(get_db), principal: Principal = Depends(require_authenticated)):
    return [project_summary(p) for p in session.scalars(select(ProjectModel).order_by(ProjectModel.created_at.desc()).limit(100))]


@router.get("/{project_id}")
def get_project(project_id: str, session: Session = Depends(get_db), principal: Principal = Depends(require_authenticated)):
    project = session.get(ProjectModel, project_id)
    if not project:
        raise HTTPException(404, detail="Project not found")
    return project_summary(project)


@router.get("/{project_id}/releases")
def list_releases(project_id: str, session: Session = Depends(get_db), principal: Principal = Depends(require_authenticated)):
    if not session.get(ProjectModel, project_id):
        raise HTTPException(404, detail="Project not found")
    return [release_summary(r) for r in session.scalars(select(ProjectReleaseModel)
        .where(ProjectReleaseModel.project_id == project_id).order_by(ProjectReleaseModel.version.desc()))]


@router.patch("/{project_id}")
def rename_project(project_id: str, request: RenameProjectRequest, session: Session = Depends(get_db),
                   admin: AdminPrincipal = Depends(require_admin)):
    name = request.name.strip()
    if not name:
        raise HTTPException(422, detail="Project name cannot be blank")
    project = session.scalar(select(ProjectModel).where(ProjectModel.project_id == project_id).with_for_update())
    previous = project.name if project else None
    if not project:
        root = session.scalar(select(WorkflowRunModel).where(WorkflowRunModel.project_id == project_id)
            .order_by(WorkflowRunModel.created_at, WorkflowRunModel.run_id).limit(1))
        if not root:
            raise HTTPException(404, detail="Project not found")
        project = ProjectModel(project_id=project_id, root_run_id=root.run_id, name=name)
        session.add(project)
    else:
        project.name = name
    AuditRepository.record(session, actor=admin.actor_id, action="PROJECT_RENAMED", target=project_id,
        run_id=project.root_run_id, result="SUCCESS", details={"previous_name": previous, "name": name})
    session.commit()
    return project_summary(project)


@router.get("/{project_id}/releases/{release_id}")
def get_release(project_id: str, release_id: str, session: Session = Depends(get_db),
                principal: Principal = Depends(require_authenticated)):
    release = session.get(ProjectReleaseModel, release_id)
    if not release or release.project_id != project_id:
        raise HTTPException(404, detail="Release not found")
    return release_summary(release, include_manifest=True)


def _download_report(session, project_id, release):
    from hashlib import sha256
    if not release or release.project_id != project_id:
        raise HTTPException(404, detail="Release not found")
    report = (release.manifest or {}).get("report")
    artifact = session.get(ArtifactModel, report["artifact_id"]) if report else None
    if not artifact or artifact.run_id != release.run_id:
        raise HTTPException(404, detail="No final report exists for this release")
    path = artifact_path(ArtifactStoreService(), session, artifact)
    if not path or not path.is_file():
        raise HTTPException(404, detail="Final report file not found")
    if sha256(path.read_bytes()).hexdigest() != report["checksum"]:
        raise HTTPException(409, detail="Final report checksum mismatch")
    return FileResponse(path, media_type=DOCX_MEDIA_TYPE, filename=artifact.name,
                        headers={"X-Checksum-SHA256": report["checksum"]})


@router.get("/{project_id}/report")
def download_current_report(project_id: str, session: Session = Depends(get_db),
                            principal: Principal = Depends(require_authenticated)):
    project = session.get(ProjectModel, project_id)
    if not project:
        raise HTTPException(404, detail="Project not found")
    if not project.current_release_id:
        raise HTTPException(409, detail="Project has no accepted release yet")
    return _download_report(session, project_id, session.get(ProjectReleaseModel, project.current_release_id))


@router.get("/{project_id}/releases/{release_id}/report")
def download_release_report(project_id: str, release_id: str, session: Session = Depends(get_db),
                            principal: Principal = Depends(require_authenticated)):
    return _download_report(session, project_id, session.get(ProjectReleaseModel, release_id))


@router.get("/{project_id}/changes")
def list_changes(project_id: str, session: Session = Depends(get_db), principal: Principal = Depends(require_authenticated)):
    runs = session.scalars(select(WorkflowRunModel).where(WorkflowRunModel.project_id == project_id)
        .order_by(WorkflowRunModel.created_at.desc()).limit(100))
    return [{"run_id": r.run_id, "brief": r.brief, "status": r.status, "metadata": r.run_metadata}
            for r in runs if (r.run_metadata or {}).get("run_kind") == "change"]


@router.post("/{project_id}/changes", status_code=201)
def create_change(project_id: str, request: CreateChangeRequest, background_tasks: BackgroundTasks,
                  session: Session = Depends(get_db), publisher=Depends(get_publisher),
                  admin: AdminPrincipal = Depends(require_admin)):
    try:
        run = ChangeRequestService(publisher=publisher).create(session, project_id, request.brief, admin.actor_id,
            request.module_plan.model_dump() if request.module_plan else None, request.base_run_id, request.title)
    except ChangeRequestError as exc:
        session.rollback()
        raise HTTPException(exc.status_code, detail=str(exc)) from exc
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, detail="Project version/change reservation changed; reload the project") from exc
    if run.status == "PLANNING":
        background_tasks.add_task(_background_change_plan, run.run_id)
    return {"project_id": run.project_id, "run_id": run.run_id, "status": run.status,
        "base_run_id": run.run_metadata["base_run_id"], "base_release_id": run.run_metadata["base_release_id"],
        "target_version": run.run_metadata["target_version"]}
