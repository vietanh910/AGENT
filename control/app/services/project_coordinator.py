"""Durable, sequential project execution with a Linear planning barrier."""

from copy import deepcopy
from datetime import timedelta, timezone
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

from control.app.config import settings
from control.app.database.models import ApprovalModel, ArtifactModel, TaskDependencyModel, TaskModel, WorkflowRunModel, utc_now
from control.app.database.repositories import AuditRepository, LinearLinkRepository, TaskRepository
from control.app.services.artifact_store import ArtifactStoreError, select_input_artifacts
from control.app.services.plan_service import PlanService
from control.app.services.task_manager import TaskManager
from agent_contract.quality import quality_verdict


class ProjectCoordinator:
    def __init__(self, session_factory, dispatcher, linear_sync, artifact_store=None, recovery=None):
        self.session_factory = session_factory
        self.dispatcher = dispatcher
        self.linear = linear_sync
        self.artifact_store = artifact_store
        from control.app.services.project_recovery import ProjectRecoveryService
        self.recovery = recovery or ProjectRecoveryService(artifact_store)

    def _sync_backlog(self, session, run, tasks) -> bool:
        """Create project, module milestones, issues and relations before any dispatch."""
        if (run.run_metadata or {}).get("linear_plan_ready"):
            return True
        if self.linear is None:
            return False
        events = []

        class Collector:
            def enqueue_change(self, **event):
                events.append(event)
                return True

        deps = session.execute(select(TaskDependencyModel.task_id, TaskDependencyModel.depends_on_task_id)
            .join(TaskModel, TaskModel.task_id == TaskDependencyModel.task_id)
            .where(TaskModel.run_id == run.run_id)).all()
        PlanService(linear_sync_service=Collector())._sync_plan_to_linear(session, run, run.plan, tasks, deps)
        synced = 0
        for event in events:
            if LinearLinkRepository.get_link(session, event["entity_type"], event["local_id"]):
                continue
            if synced >= 8:
                return False
            if not self.linear.sync_entity_now(session, **event):
                return False
            synced += 1
        run.run_metadata = {**(run.run_metadata or {}), "linear_plan_ready": True,
                            "linear_dry_run": settings.LINEAR_DRY_RUN}
        session.commit()
        return True

    def _block(self, session, run, reason):
        if settings.PM_AUTO_RECOVERY:
            # A lost source for report finalization may still be available in the recovery copy.
            for task in session.scalars(select(TaskModel).where(TaskModel.run_id == run.run_id)):
                for ref in (task.result or {}).get("artifacts", []):
                    artifact = session.get(ArtifactModel, ref.get("artifact_id"))
                    if artifact and (artifact.metadata_json or {}).get("file_path"):
                        if not self.recovery.restore(session, artifact) and artifact.artifact_id in str(reason):
                            self.recovery.recover_task(session, run, task, reason)
                            return
            self.recovery.wait(session, run, reason)
            return
        run.status = "BLOCKED"
        run.run_metadata = {**(run.run_metadata or {}), "block_reason": reason}
        AuditRepository.record(session, actor="PM_COORDINATOR", action="PROJECT_BLOCKED",
                               target=run.run_id, run_id=run.run_id, result="BLOCKED", details={"reason": reason})
        session.commit()

    def _completion_error(self, session, task):
        """A callback alone is not proof that its deliverables are available for handoff."""
        if task.dispatch_state != "COMPLETED" or (task.context or {}).get("quality_handled"):
            return None
        expected = (task.context or {}).get("output_artifact_type")
        if expected and not session.scalar(select(ArtifactModel.artifact_id).where(
            ArtifactModel.run_id == task.run_id, ArtifactModel.task_id == task.task_id, ArtifactModel.type == expected)):
            return f"Missing {expected} from completed task {task.task_id}"
        declared = (task.result or {}).get("artifacts") or []
        if not isinstance(declared, list):
            return f"Invalid output artifact list: {task.task_id}"
        if expected and not any(isinstance(ref, dict) and ref.get("type") == expected for ref in declared):
            return f"Missing {expected} reference from completed task {task.task_id}"
        for ref in declared:
            artifact = session.get(ArtifactModel, ref.get("artifact_id")) if isinstance(ref, dict) else None
            if not artifact or artifact.run_id != task.run_id or artifact.task_id != task.task_id or artifact.created_by != task.agent_id:
                return f"Output artifact is not stored for task {task.task_id}"
            for key in ("uri", "type", "checksum", "name", "version", "created_by"):
                if ref.get(key) is not None and ref[key] != getattr(artifact, key):
                    return f"Output artifact {artifact.artifact_id} has mismatched {key}"
            file_path = (artifact.metadata_json or {}).get("file_path")
            if file_path and not Path(file_path).is_file():
                return f"Output artifact file is missing: {artifact.artifact_id}"
            if file_path and artifact.checksum and sha256(Path(file_path).read_bytes()).hexdigest() != artifact.checksum:
                return f"Output artifact checksum mismatch: {artifact.artifact_id}"
        if task.action in {"run_test_suite", "security_review"} and quality_verdict(task.result) is None:
            return f"Missing quality verdict: {task.task_id}"
        return None

    def _recover_quality_source(self, session, run, source):
        if settings.PM_AUTO_RECOVERY:
            for producer_id in (source.context or {}).get("input_artifact_sources", {}).get("SOURCE_CODE", []):
                producer = session.get(TaskModel, producer_id)
                if producer and producer.run_id == run.run_id:
                    producer.context = {**(producer.context or {}), "findings": source.result}
                    self.recovery.retry(session, run, producer, "Quality repair needs another implementation attempt")
                    return True
        return False

    def _schedule_fix(self, session, run, source):
        context = deepcopy(source.context or {})
        loops = int((run.run_metadata or {}).get("fix_loop_count", 0))
        if loops >= settings.MAX_FIX_LOOPS:
            if self._recover_quality_source(session, run, source):
                return
            self._block(session, run, f"MAX_FIX_LOOPS={settings.MAX_FIX_LOOPS}: {source.task_id}")
            return
        if self.linear and not self.linear.sync_entity_now(session, entity_type="RESULT", local_id=source.task_id,
                run_id=run.run_id, data={**(source.result or {}), "status": "BLOCKED", "role": source.role,
                    "summary": "Quality check failed; PM is scheduling repair and a new check."}):
            return
        loops += 1
        # Compact IDs remain valid even for long run IDs.
        import hashlib
        tag = hashlib.sha256(f"{run.run_id}:{source.task_id}:{loops}".encode()).hexdigest()[:16]
        fix_id, retest_id, resec_id = f"fix-{tag}", f"test-{tag}", f"sec-{tag}"
        inputs = deepcopy(context.get("input_artifact_sources", {}))
        report_type = "SECURITY_REPORT" if source.role == "SECURITY" else "TEST_REPORT"
        inputs[report_type] = [source.task_id]
        baseline_inputs = deepcopy(context.get("baseline_artifact_ids", {}))
        common = {"module_id": context.get("module_id"), "module_name": context.get("module_name"),
                  "milestone_key": context.get("milestone_key"), "epic": context.get("epic"),
                  "baseline_artifact_ids": baseline_inputs,
                  "work_item_id": context.get("work_item_id"), "repair_of": source.task_id}
        definitions = [{
            "task_id": fix_id, "role": "DEV", "action": "fix_bugs",
            "title": f"Fix: {context.get('title', source.task_id)}",
            "instruction": "Fix the supplied findings; preserve all completed modules and return complete cumulative source.",
            "depends_on": [source.task_id], "output_artifact_type": "SOURCE_CODE",
            "input_artifact_types": list(dict.fromkeys([*inputs, *baseline_inputs])), "acceptance_criteria": ["All reported failures fixed"],
            "context": {**common, "input_artifact_sources": inputs, "findings": source.result},
        }]
        test_inputs = {kind: ids for kind, ids in inputs.items() if kind not in {"TEST_REPORT", "SECURITY_REPORT"}}
        test_inputs["SOURCE_CODE"] = [fix_id]
        if source.role == "SECURITY" and (run.plan or {}).get("run_kind") == "change":
            regression = next((t for t in session.scalars(select(TaskModel).where(TaskModel.run_id == run.run_id))
                               if (t.context or {}).get("title") == "Full project regression"), None)
            if regression:
                test_inputs["TEST_CASE"] = regression.context["input_artifact_sources"]["TEST_CASE"]
                baseline_inputs["TEST_CASE"] = regression.context.get("baseline_artifact_ids", {}).get("TEST_CASE", [])
        definitions.append({
            "task_id": retest_id, "role": "TESTER", "action": "run_test_suite", "title": "Retest repaired source",
            "instruction": "Run the failed cases and regression suite on the exact supplied source. Return verdict PASS or FAIL.",
            "depends_on": [fix_id], "output_artifact_type": "TEST_REPORT", "input_artifact_types": list(dict.fromkeys([*test_inputs, *baseline_inputs])),
            "acceptance_criteria": context.get("acceptance_criteria", ["Tests pass"]),
            "context": {**common, "input_artifact_sources": test_inputs},
        })
        final_id = retest_id
        if source.role == "SECURITY":
            definitions.append({
                "task_id": resec_id, "role": "SECURITY", "action": "security_review", "title": "Review repaired source",
                "instruction": "Review repaired source and return verdict PASS or FAIL with security findings.",
                "depends_on": [retest_id], "output_artifact_type": "SECURITY_REPORT",
                "input_artifact_types": list(dict.fromkeys([*test_inputs, *baseline_inputs])), "acceptance_criteria": ["No HIGH or CRITICAL findings"],
                "context": {**common, "input_artifact_sources": test_inputs},
            })
            final_id = resec_id
        tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id == run.run_id)))
        if "TEST_CASE" not in test_inputs:
            cases = [t for t in tasks if t.action == "create_test_cases" and t.dispatch_state == "COMPLETED"]
            if cases:
                definitions[1]["context"]["input_artifact_sources"]["TEST_CASE"] = [cases[-1].task_id]
                definitions[1]["input_artifact_types"].append("TEST_CASE")
        if len(tasks) + len(definitions) > settings.MAX_TASKS_PER_RUN:
            if self._recover_quality_source(session, run, source):
                return
            self._block(session, run, "Repair would exceed MAX_TASKS_PER_RUN")
            return
        # Redirect every consumer, including the next feature/module, to the successful recheck.
        dependents = list(session.scalars(select(TaskDependencyModel).where(
            TaskDependencyModel.depends_on_task_id == source.task_id)))
        old_code_ids = set(context.get("input_artifact_sources", {}).get("SOURCE_CODE", []))
        for task in tasks:
            if task.dispatch_state not in {"QUEUED", "RETRY_WAIT"}:
                continue
            ctx = deepcopy(task.context or {})
            bindings = ctx.get("input_artifact_sources", {})
            for kind, ids in bindings.items():
                bindings[kind] = [fix_id if kind == "SOURCE_CODE" and i in old_code_ids else
                                 (final_id if i == source.task_id else i) for i in ids]
            task.context = ctx
        TaskManager.create_tasks_from_plan(session, run.run_id, definitions)
        for dependency in dependents:
            task_id = dependency.task_id
            session.delete(dependency)
            session.flush()
            TaskRepository.add_dependency(session, task_id=task_id, depends_on_task_id=final_id)
        source.context = {**context, "quality_handled": True, "repair_task_id": final_id}
        run.run_metadata = {**(run.run_metadata or {}), "fix_loop_count": loops, "linear_plan_ready": False}
        # Repair definitions also form part of the persisted, reviewable execution plan.
        plan = deepcopy(run.plan)
        plan.setdefault("repairs", []).extend(definitions)
        run.plan = plan
        session.commit()

    def tick(self, run_id: str):
        with self.session_factory() as session:
            run = session.scalar(select(WorkflowRunModel).where(WorkflowRunModel.run_id == run_id).with_for_update())
            if not run:
                return
            if settings.PM_AUTO_RECOVERY and run.status == "BLOCKED" and (run.plan or {}).get("execution_mode") == "modules":
                run.status = "RUNNING"
            if run.status not in {"RUNNING", "AWAITING_APPROVAL"}:
                return
            if settings.PM_AUTO_RECOVERY and self.recovery.waiting(run):
                return
            tasks = list(session.scalars(select(TaskModel).where(TaskModel.run_id == run_id).order_by(TaskModel.created_at)))
            if not self._sync_backlog(session, run, tasks):
                if settings.PM_AUTO_RECOVERY and run.status == "RUNNING":
                    self.recovery.wait(session, run, "Waiting for Linear backlog synchronization")
                return
            if run.status != "RUNNING":
                return
            completion_errors = {t.task_id: self._completion_error(session, t) for t in tasks}
            # Sync observable task state from DB, so a Redis outage cannot lose progress.
            if self.linear:
                for task in tasks:
                    failed_quality = (task.action in {"run_test_suite", "security_review"}
                                      and quality_verdict(task.result) == "FAIL" and not (task.context or {}).get("quality_repaired"))
                    if not self.linear.sync_entity_now(session, entity_type="STATUS", local_id=task.task_id,
                            run_id=run_id, data={"task_id": task.task_id, "role": task.role,
                                                "dispatch_state": "BLOCKED" if failed_quality or completion_errors[task.task_id] else task.dispatch_state}):
                        return
            for task in tasks:
                if task.dispatch_state == "COMPLETED" and not (task.context or {}).get("quality_handled"):
                    completion_error = completion_errors[task.task_id]
                    if completion_error:
                        if settings.PM_AUTO_RECOVERY:
                            self.recovery.recover_task(session, run, task, completion_error)
                            return
                        task.context = {**(task.context or {}), "completion_error": completion_error}
                        if self.linear:
                            self.linear.sync_entity_now(session, entity_type="RESULT", local_id=task.task_id,
                                run_id=run_id, data={"status": "BLOCKED", "role": task.role, "summary": completion_error})
                        self._block(session, run, completion_error)
                        return
                    quality = task.action in {"run_test_suite", "security_review"}
                    verdict = quality_verdict(task.result)
                    if quality and verdict == "FAIL":
                        self._schedule_fix(session, run, task)
                        return
                    if self.linear:
                        if not self.linear.sync_entity_now(session, entity_type="RESULT", local_id=task.task_id,
                            run_id=run_id, data={"task_id": task.task_id, "role": task.role, "action": task.action,
                                                **(task.result or {})}):
                            return
                        for artifact in (task.result or {}).get("artifacts", []):
                            if not self.linear.sync_entity_now(session, entity_type="ARTIFACT", local_id=artifact["artifact_id"],
                                run_id=run_id, data={**artifact, "task_id": task.task_id}):
                                return
                    task.context = {**(task.context or {}), "quality_handled": True}
                    task.context = {key: value for key, value in task.context.items() if key != "completion_error"}
                    # Resolve the failed check's Linear issue only after its replacement passed.
                    child = task
                    for _ in range(settings.MAX_FIX_LOOPS + 1):
                        parent_id = (child.context or {}).get("repair_of")
                        parent = session.get(TaskModel, parent_id) if parent_id else None
                        if not parent or (parent.context or {}).get("repair_task_id") != child.task_id:
                            break
                        parent.context = {**(parent.context or {}), "quality_repaired": True}
                        if self.linear:
                            self.linear.sync_entity_now(session, entity_type="RESULT", local_id=parent.task_id,
                                run_id=run_id, data={"status": "COMPLETED", "role": parent.role,
                                    "summary": f"Original findings repaired and verified by {child.task_id}"})
                        child = parent
                elif task.dispatch_state in {"BLOCKED", "FAILED"}:
                    if settings.PM_AUTO_RECOVERY:
                        self.recovery.recover_task(session, run, task, str(task.error or task.result or task.dispatch_state))
                        return
                    self._block(session, run, f"Task {task.task_id}: {task.dispatch_state}")
                    return
            session.commit()
            if tasks and all(t.dispatch_state == "COMPLETED" for t in tasks):
                from control.app.services.project_releases import publish_release, ReleaseError
                try:
                    publish_release(session, run, self.artifact_store)
                except ReleaseError as exc:
                    session.rollback()
                    run = session.get(WorkflowRunModel, run_id)
                    self._block(session, run, str(exc))
                    return
                run.status, run.completed_at = "COMPLETED", utc_now()
                run.run_metadata = {k: v for k, v in (run.run_metadata or {}).items() if k not in {"recovery", "block_reason"}}
                session.commit()
                return
            if any(t.dispatch_state in {"ASSIGNED", "RUNNING"} for t in tasks):
                if settings.PM_AUTO_RECOVERY:
                    for task in tasks:
                        if task.dispatch_state not in {"ASSIGNED", "RUNNING"}:
                            continue
                        deadline = task.deadline or task.updated_at + timedelta(seconds=task.timeout_seconds)
                        if deadline.replace(tzinfo=deadline.tzinfo or timezone.utc) <= utc_now():
                            self.recovery.recover_task(session, run, task, "Agent execution deadline exceeded")
                            return
                return
            ready = TaskManager.get_ready_tasks(session, run_id)
            if not ready:
                return
            task = ready[0]
            run.current_stage = (task.context or {}).get("module_id") or task.action
            if task.role == "GATE":
                approvals = list(session.scalars(select(ApprovalModel).where(ApprovalModel.run_id == run_id)))
                matching = [a for a in approvals if a.gate.lower() == task.action.lower()]
                if any(a.status == "APPROVED" for a in matching):
                    task.dispatch_state = "COMPLETED"
                else:
                    run.status = "AWAITING_APPROVAL"
                session.commit()
                return
            try:
                refs = select_input_artifacts(session, task)
                if settings.PM_AUTO_RECOVERY:
                    for ref in refs:
                        artifact = session.get(ArtifactModel, ref.artifact_id)
                        if (artifact.metadata_json or {}).get("file_path") and not self.recovery.restore(session, artifact):
                            raise ArtifactStoreError(f"Input file unavailable: {ref.artifact_id}")
            except ArtifactStoreError as exc:
                if settings.PM_AUTO_RECOVERY:
                    self.recovery.recover_task(session, run, task, str(exc))
                    return
                self._block(session, run, str(exc))
                return
            if settings.PM_AUTO_RECOVERY and (task.context or {}).get("pm_takeover"):
                try:
                    self.recovery.execute_pm(session, run, task)
                except Exception as exc:
                    session.rollback()
                    task = session.get(TaskModel, task.task_id)
                    session.refresh(run)
                    if run.status != "RUNNING":
                        return
                    task.dispatch_state, task.deadline = "QUEUED", None
                    self.recovery.wait(session, run, f"PM takeover: {type(exc).__name__}: {exc}", task)
                return
            dispatched = self.dispatcher.dispatch_single_task(session, task)
            if not dispatched and settings.PM_AUTO_RECOVERY and task.dispatch_state in {"QUEUED", "RETRY_WAIT"}:
                from datetime import datetime
                first_stall = (task.context or {}).get("dispatch_stalled_since")
                stalled_at = datetime.fromisoformat(first_stall) if first_stall else utc_now()
                if not first_stall:
                    task.context = {**(task.context or {}), "dispatch_stalled_since": stalled_at.isoformat()}
                    session.commit()
                if utc_now() - stalled_at >= timedelta(seconds=settings.PM_STALL_SECONDS):
                    self.recovery.recover_task(session, run, task, "Agent/worker unavailable or dispatch stalled")
