"""Read-only dashboard projections from committed state, including physical artifact locations."""

from datetime import timezone
from hashlib import sha256
import json
from pathlib import Path
import socket

from sqlalchemy import func, select

from agent_contract.registry import AGENT_REGISTRY_DEFAULT
from agent_contract.quality import quality_verdict
from control.app.config import settings
from control.app.database.models import (
    AgentEventModel, AgentModel, ArtifactModel, AuditLogModel, ProjectModel, TaskDependencyModel, TaskModel,
    WorkerModel, WorkflowRunModel, utc_now,
)
from control.app.observability.logging import mask_sensitive_data
from control.app.services import artifact_store
from control.app.services.project_names import project_display_name


def iso(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat() if value else None


def text(value, limit=1200):
    return mask_sensitive_data(str(value or ""))[:limit]


def physical_path(artifact):
    metadata = artifact.metadata_json or {}
    if metadata.get("file_path"):
        return Path(metadata["file_path"]).resolve()
    if artifact.uri.startswith("artifact://"):
        return (artifact_store.DEFAULT_ARTIFACT_STORE_DIR / (artifact.run_id or "global") /
                f"{artifact.artifact_id}-{artifact_store.sanitize_filename(artifact.name)}").resolve()
    return None


def file_summary(artifact, agent_map, task_map):
    path = physical_path(artifact)
    try:
        available = bool(path and path.is_file())
        size = path.stat().st_size if available else (artifact.metadata_json or {}).get("size_bytes")
    except OSError:
        available, size = False, (artifact.metadata_json or {}).get("size_bytes")
    agent, task = agent_map.get(artifact.created_by), task_map.get(artifact.task_id)
    return {"artifact_id": artifact.artifact_id, "name": artifact.name, "type": artifact.type,
        "created_by": artifact.created_by,
        "source_machine": (artifact.metadata_json or {}).get("source_machine") or (agent.machine if agent else None),
        "source_worker_id": (artifact.metadata_json or {}).get("source_worker_id"),
        "run_id": artifact.run_id, "task_id": artifact.task_id,
        "task_title": text((task.context or {}).get("title") or task.action) if task else None,
        "module_id": (task.context or {}).get("module_id") if task else None,
        "received_at": iso(artifact.created_at), "size_bytes": size, "checksum": artifact.checksum,
        "local_path": str(path) if path else None, "available": available,
        "storage_status": "STORED" if available else ("MISSING" if path else "REFERENCE"),
        "uri": artifact.uri, "recovered_by_pm": bool((artifact.metadata_json or {}).get("recovered_by_pm")),
        "download_url": f"/api/v1/dashboard/artifacts/{artifact.artifact_id}/content" if available else None}


def task_status(task):
    context = task.context or {}
    if task.dispatch_state == "COMPLETED" and (context.get("completion_error") or (
            task.action in {"run_test_suite", "security_review"} and quality_verdict(task.result) != "PASS"
            and not context.get("quality_repaired"))):
        return "BLOCKED"
    if task.dispatch_state == "QUEUED" and context.get("pm_takeover"):
        return "RETRY_WAIT"
    return task.dispatch_state


def task_summary(task):
    context = task.context or {}
    return {"task_id": task.task_id, "run_id": task.run_id, "agent_id": task.agent_id,
        "role": task.role, "action": task.action, "title": text(context.get("title") or task.action),
        "instruction": text(task.instruction, 2500), "module_id": context.get("module_id"),
        "module_name": context.get("module_name"), "epic": context.get("epic"),
        "status": task_status(task), "attempts": task.attempts, "deadline": iso(task.deadline),
        "updated_at": iso(task.updated_at), "summary": text((task.result or {}).get("summary")),
        "error": text((task.error or {}).get("message")),
        "recovery_count": context.get("pm_recovery_count", 0), "pm_takeover": bool(context.get("pm_takeover")),
        "original_role": (context.get("recovery_original") or {}).get("role"),
        "input_artifact_sources": context.get("input_artifact_sources", {}),
        "baseline_artifact_ids": context.get("baseline_artifact_ids", {}),
        "output_artifact_type": context.get("output_artifact_type"),
        "acceptance_criteria": [text(item) for item in context.get("acceptance_criteria", [])]}


def project_catalog(session, run_id=None, project_id=None):
    """List projects once; load version history only for the selected project."""
    grouped = session.execute(select(WorkflowRunModel.project_id,
        func.max(WorkflowRunModel.created_at).label("last_created"), func.count().label("run_count"))
        .group_by(WorkflowRunModel.project_id).order_by(func.max(WorkflowRunModel.created_at).desc()).limit(100)).all()
    ids = [row.project_id for row in grouped]
    selected = session.get(WorkflowRunModel, run_id) if run_id else None
    if run_id and (not selected or (project_id and selected.project_id != project_id)):
        raise LookupError("Run not found in project")
    project_id = project_id or (selected.project_id if selected else None)
    if project_id and project_id not in ids:
        if not session.scalar(select(WorkflowRunModel.run_id).where(WorkflowRunModel.project_id == project_id).limit(1)):
            raise LookupError("Project not found")
        ids.append(project_id)
    records = {p.project_id: p for p in session.scalars(select(ProjectModel).where(ProjectModel.project_id.in_(ids)))} if ids else {}
    latest_times = select(WorkflowRunModel.project_id, func.max(WorkflowRunModel.created_at).label("at"))\
        .where(WorkflowRunModel.project_id.in_(ids)).group_by(WorkflowRunModel.project_id).subquery()
    earliest_times = select(WorkflowRunModel.project_id, func.min(WorkflowRunModel.created_at).label("at"))\
        .where(WorkflowRunModel.project_id.in_(ids)).group_by(WorkflowRunModel.project_id).subquery()
    latest, roots = {}, {}
    for row in session.scalars(select(WorkflowRunModel).join(latest_times,
            (WorkflowRunModel.project_id == latest_times.c.project_id) & (WorkflowRunModel.created_at == latest_times.c.at))
            .order_by(WorkflowRunModel.run_id.desc())):
        latest.setdefault(row.project_id, row)
    for row in session.scalars(select(WorkflowRunModel).join(earliest_times,
            (WorkflowRunModel.project_id == earliest_times.c.project_id) & (WorkflowRunModel.created_at == earliest_times.c.at))
            .order_by(WorkflowRunModel.run_id)):
        roots.setdefault(row.project_id, row)
    root_ids = {p.root_run_id for p in records.values()}
    if root_ids:
        for row in session.scalars(select(WorkflowRunModel).where(WorkflowRunModel.run_id.in_(root_ids))):
            roots[row.project_id] = row
    counts = {row.project_id: row.run_count for row in grouped}
    for pid in ids:
        if pid not in counts:
            counts[pid] = session.scalar(select(func.count()).select_from(WorkflowRunModel).where(WorkflowRunModel.project_id == pid))
    projects = [{"project_id": pid, "name": text(project_display_name(pid, records.get(pid), roots.get(pid), latest.get(pid)), 255),
                 "status": latest[pid].status, "run_count": counts.get(pid, 1)} for pid in ids if pid in latest]
    if not project_id:
        choice = next((p for p in projects if p["status"] in {"RUNNING", "PLANNING", "AWAITING_APPROVAL", "BLOCKED"}), projects[0] if projects else None)
        project_id = choice["project_id"] if choice else None
    history = list(session.scalars(select(WorkflowRunModel).where(WorkflowRunModel.project_id == project_id)
        .order_by(WorkflowRunModel.created_at.desc(), WorkflowRunModel.run_id.desc()).limit(100))) if project_id else []
    selected = selected or next((r for r in history if r.status in {"RUNNING", "PLANNING", "AWAITING_APPROVAL", "BLOCKED"}), history[0] if history else None)
    if selected and all(row.run_id != selected.run_id for row in history):
        history.append(selected)
    project = next((p for p in projects if p["project_id"] == project_id), None)
    return projects, project, history, selected


def dashboard_snapshot(session, run_id=None, project_id=None, view="system"):
    now = utc_now()
    host = socket.gethostname()
    agents = list(session.scalars(select(AgentModel).order_by(AgentModel.agent_id)))
    agent_map = {agent.agent_id: agent for agent in agents}
    workers = list(session.scalars(select(WorkerModel).order_by(WorkerModel.machine_id, WorkerModel.worker_id)))
    worker_data, machine_workers = [], {}
    for worker in workers:
        last_seen = worker.last_heartbeat_at or worker.registered_at
        age = (now - last_seen.replace(tzinfo=last_seen.tzinfo or timezone.utc)).total_seconds() if last_seen else None
        status = "ONLINE" if worker.status == "ONLINE" and age is not None and age <= settings.HEARTBEAT_TIMEOUT_SECONDS else "OFFLINE"
        item = {"worker_id": worker.worker_id, "machine_id": worker.machine_id, "status": status,
                "last_seen": iso(last_seen), "agents": [a.agent_id for a in agents if a.machine == worker.machine_id]}
        worker_data.append(item)
        if worker.machine_id not in machine_workers or status == "ONLINE":
            machine_workers[worker.machine_id] = item
    projects, selected_project, runs, selected = project_catalog(session, run_id, project_id)
    scoped = view == "project"
    project_id = selected.project_id if selected else None
    project_runs = select(WorkflowRunModel.run_id).where(WorkflowRunModel.project_id == project_id)
    project_run_ids = set(session.scalars(project_runs)) if scoped else set()
    tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id == selected.run_id)
        .order_by(TaskModel.created_at, TaskModel.task_id))) if selected else []
    active = list(session.scalars(select(TaskModel).where(TaskModel.dispatch_state.in_(["ASSIGNED", "RUNNING"]))
        .order_by(TaskModel.updated_at.desc())))
    # Local PM takeover writes task state directly, independently of a worker heartbeat.
    active_by_agent = {}
    for task in active:
        if not scoped or task.run_id in project_run_ids:
            active_by_agent.setdefault(task.agent_id, task)
    waiting_by_agent = {}
    for task in tasks:
        if task_status(task) in {"BLOCKED", "FAILED", "RETRY_WAIT"}:
            waiting_by_agent.setdefault(task.agent_id, task)
    project_agent_ids = set(session.scalars(select(TaskModel.agent_id).where(TaskModel.run_id.in_(project_runs)).distinct())) if scoped else None
    busy_elsewhere = {task.agent_id for task in active if task.agent_id not in active_by_agent} if scoped else set()
    agent_data = []
    defaults = {a["agent_id"]: a for a in AGENT_REGISTRY_DEFAULT}
    for agent_id in [*defaults, *(aid for aid in agent_map if aid not in defaults)]:
        if scoped and agent_id not in project_agent_ids:
            continue
        agent, default = agent_map.get(agent_id), defaults.get(agent_id, {})
        role = agent.role if agent else default["role"].value
        machine = agent.machine if agent else default.get("machine")
        worker = machine_workers.get(machine)
        task = active_by_agent.get(agent_id) or waiting_by_agent.get(agent_id)
        if scoped and not task:
            task = next((t for t in tasks if t.agent_id == agent_id and t.dispatch_state in {"QUEUED", "RETRY_WAIT"}), None)
            task = task or next((t for t in reversed(tasks) if t.agent_id == agent_id), None)
        local = bool(task and task.action == "recover_task" and task.role == "PM")
        connection = "LOCAL" if local else (worker["status"] if worker and agent else "UNREGISTERED")
        status = (task_status(task) if task else agent.status if agent else "UNREGISTERED")
        if scoped and not task:
            status = "IDLE"
        if scoped and task and task.dispatch_state in {"QUEUED", "RETRY_WAIT"} and agent_id in busy_elsewhere:
            status = "WAITING_AGENT"
        if not task and status == "RUNNING":
            status = "IDLE"
        if connection in {"OFFLINE", "UNREGISTERED"} and not local:
            status = connection
        agent_data.append({"agent_id": agent_id, "role": role, "machine_id": machine,
            "status": status, "connection": connection, "execution_host": host if local else machine,
            "last_seen": iso(agent.updated_at) if agent else None,
            "task": task_summary(task) if task else None})
    # Unregistered machines are visible without pretending a worker has connected.
    known_machines = {worker["machine_id"] for worker in worker_data}
    for machine in sorted({a["machine_id"] for a in agent_data if a["machine_id"]} - known_machines):
        worker_data.append({"worker_id": None, "machine_id": machine, "status": "UNREGISTERED", "last_seen": None,
                            "agents": [a["agent_id"] for a in agent_data if a["machine_id"] == machine]})
    if scoped:
        machines = {a["machine_id"] for a in agent_data}
        worker_data = [{**worker, "agents": [aid for aid in worker["agents"] if aid in project_agent_ids]}
                       for worker in worker_data if worker["machine_id"] in machines]
    files = list(session.scalars(select(ArtifactModel).where(ArtifactModel.run_id == selected.run_id)
        .order_by(ArtifactModel.created_at.desc(), ArtifactModel.artifact_id.desc()).limit(200))) if selected else []
    recent_query = select(ArtifactModel)
    if scoped:
        recent_query = recent_query.where(ArtifactModel.run_id.in_(project_runs))
    recent = list(session.scalars(recent_query.order_by(ArtifactModel.created_at.desc(), ArtifactModel.artifact_id.desc()).limit(80)))
    file_task_ids = {a.task_id for a in [*files, *recent] if a.task_id}
    task_map = {task.task_id: task for task in tasks}
    if file_task_ids:
        task_map.update({task.task_id: task for task in session.scalars(select(TaskModel).where(TaskModel.task_id.in_(file_task_ids)))})
    # Restore provenance even if a producer was reassigned to PM afterwards.
    all_files = {a.artifact_id: file_summary(a, agent_map, task_map) for a in [*files, *recent]}
    edges = [{"from": parent, "to": child} for child, parent in session.execute(
        select(TaskDependencyModel.task_id, TaskDependencyModel.depends_on_task_id)
        .where(TaskDependencyModel.task_id.in_([t.task_id for t in tasks]),
               TaskDependencyModel.depends_on_task_id.in_([t.task_id for t in tasks])))] if tasks else []
    activity = []
    audit_query = select(AuditLogModel)
    event_query = select(AgentEventModel)
    if scoped:
        audit_query = audit_query.where(AuditLogModel.run_id.in_(project_runs))
        event_query = event_query.where(AgentEventModel.run_id.in_(project_runs))
    for audit in session.scalars(audit_query.order_by(AuditLogModel.id.desc()).limit(40)):
        activity.append({"id": f"audit-{audit.id}", "type": audit.action, "actor": audit.actor,
            "target": text(audit.target, 200), "status": audit.result, "at": iso(audit.timestamp)})
    for event in session.scalars(event_query.order_by(AgentEventModel.id.desc()).limit(40)):
        activity.append({"id": f"event-{event.id}", "type": event.event_type,
            "actor": (event.payload or {}).get("agent_id") or event.worker_id or
                (task_map[event.task_id].agent_id if event.task_id in task_map else None),
            "target": event.task_id or event.run_id, "status": None, "at": iso(event.created_at)})
    for artifact in recent:
        if not scoped or artifact.run_id in project_run_ids:
            activity.append({"id": f"file-{artifact.artifact_id}", "type": "ARTIFACT_RECEIVED", "actor": artifact.created_by,
                "target": artifact.name, "status": all_files[artifact.artifact_id]["storage_status"], "at": iso(artifact.created_at)})
    counts = {state: sum(task_status(t) == state for t in tasks) for state in {
        "QUEUED", "ASSIGNED", "RUNNING", "RETRY_WAIT", "COMPLETED", "BLOCKED", "FAILED", "CANCELLED"}}
    selected_data = None
    if selected:
        metadata = selected.run_metadata or {}
        recovery = metadata.get("recovery")
        selected_data = {"run_id": selected.run_id, "project_id": selected.project_id,
            "name": selected_project["name"], "status": selected.status,
            "brief": text(selected.brief, 2500), "current_stage": selected.current_stage,
            "recovery": {**recovery, "reason": text(recovery.get("reason"))} if recovery else None,
            "version": metadata.get("release_version") or metadata.get("target_version"),
            "report": metadata.get("project_report"), "task_counts": counts, "task_total": len(tasks),
            "progress": round(100 * counts["COMPLETED"] / len(tasks)) if tasks else 0}
    file_count_query = select(func.count()).select_from(ArtifactModel)
    active_runs_query = select(func.count()).select_from(WorkflowRunModel).where(WorkflowRunModel.status.in_(["RUNNING", "PLANNING", "AWAITING_APPROVAL"]))
    if scoped:
        file_count_query = file_count_query.where(ArtifactModel.run_id.in_(project_runs))
        active_runs_query = active_runs_query.where(WorkflowRunModel.project_id == project_id)
    payload = {"view": view, "projects": projects, "selected_project": selected_project,
        "host": {"name": host, "artifact_root": str(artifact_store.DEFAULT_ARTIFACT_STORE_DIR.resolve()),
                           "path_scope": "CONTROL_NODE", "refresh_seconds": 1},
        "summary": {"agents": len(agent_data), "active_agents": sum(bool(a["task"] and a["task"]["status"] in {"ASSIGNED", "RUNNING"}
            and a["connection"] in {"ONLINE", "LOCAL"}) for a in agent_data),
            "online_workers": sum(w["status"] == "ONLINE" for w in worker_data), "workers": len(worker_data),
            "files_received": session.scalar(file_count_query), "active_runs": session.scalar(active_runs_query)},
        "agents": agent_data, "workers": worker_data,
        "runs": [{"run_id": r.run_id, "project_id": r.project_id, "name": selected_project["name"],
                  "status": r.status, "sequence": selected_project["run_count"] - index,
                  "version": (r.run_metadata or {}).get("release_version") or (r.run_metadata or {}).get("target_version")}
                 for index, r in enumerate(runs)], "selected_run": selected_data,
        "tasks": [{**task_summary(task), "worker_connection": next(
            (a["connection"] for a in agent_data if a["agent_id"] == task.agent_id), None)} for task in tasks], "edges": edges,
        "artifacts": [all_files[a.artifact_id] for a in files], "recent_artifacts": [all_files[a.artifact_id] for a in recent],
        "activity": sorted(activity, key=lambda item: item["at"] or "", reverse=True)[:60]}
    payload["revision"] = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:20]
    payload["generated_at"] = iso(now)
    return payload
