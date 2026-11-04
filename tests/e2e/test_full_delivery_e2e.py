"""tests/e2e/test_full_delivery_e2e.py - Kiểm thử toàn diện quy trình chuyển giao phần mềm (Prompt S12).

Kịch bản:
- Dựng PC1 Control Node thật (MySQL, Session, OrchestratorEngine, Dispatcher, ResultHandler).
- Khởi tạo Worker giả lập đại diện cho toàn bộ 5 máy (PC1..PC5) và 10 Agent.
- Chạy một WorkflowRun mẫu hoàn chỉnh từ khởi tạo prompt tới COMPLETED.
- Trải qua đầy đủ các giai đoạn:
  + Scoping: PO define_backlog -> BA write_requirements -> BA write_user_stories
  + Gate: APPROVE_SCOPE (phê duyệt bởi admin)
  + Design: SA design_architecture -> DBA design_schema
  + Foundation: TESTER create_test_cases -> DEV implement_project
  + Feature Sprint: DEV implement_feature -> TESTER run_test_suite (PASS) -> SCRUM_MASTER run_sprint_review
  + Verification: SECURITY security_review (PASS)
  + Deployment: DEVOPS generate_ci_config -> DEVOPS build_image -> DEVOPS deploy -> DEVOPS health_check
  + Closure: PO accept_release -> SCRUM_MASTER run_retrospective -> PM report_status
- Sử dụng công cụ tools.check_process để thẩm định:
  + Xác nhận 100% 10 tiêu chí quy trình đều đạt PASS.
  + Xuất báo cáo Markdown docs/process-report-<run_id>.md.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import fakeredis
import pytest
from sqlalchemy import select

from agent_contract.enums import AgentRole, ArtifactType, TaskStatus
from agent_contract.models import ArtifactRef, TaskResult
from agent_contract.system_models import DispatchAck, DispatchRequest, DispatchState, RunStatus
from control.app.database.models import (
    AgentModel,
    ApprovalModel,
    ArtifactModel,
    TaskDependencyModel,
    TaskModel,
    WorkerModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import ApprovalRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.observability.audit import AuditService
from control.app.orchestrator.checkpoint import MemoryCheckpointStore
from control.app.orchestrator.engine import OrchestratorEngine
from control.app.queue.events import EventPublisher
from control.app.security.authentication import WorkerPrincipal
from control.app.services.approval import ApprovalService
from control.app.services.dispatcher import DispatcherService
from control.app.services.result_handler import ResultHandler
from control.app.services.task_manager import TaskManager
from control.app.services.worker_client import WorkerClient
from tools.check_process import generate_report_file, verify_run
from worker.fake_agent import ACTION_OUTPUT_ARTIFACT_TYPES


class E2EMockWorkerClient(WorkerClient):
    """WorkerClient mô phỏng chấp nhận task ngay lập tức (HTTP 202)."""

    def __init__(self) -> None:
        self.dispatched_requests: List[DispatchRequest] = []

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        self.dispatched_requests.append(request)
        return DispatchAck(task_id=request.task_id, accepted=True, reason="ACCEPTED_202")

    async def cancel(self, task_id: str) -> bool:
        return True

    async def get_status(self, task_id: str):
        return {"task_id": task_id, "status": "RUNNING"}


def submit_completed_result(
    session_factory,
    publisher: EventPublisher,
    task_id: str,
    agent_id: str,
    action: str,
    role: AgentRole,
    worker_id: str,
    machine_id: str,
    run_id: str,
) -> None:
    """Gửi TaskResult hoàn thành kèm ArtifactRef chuẩn về PC1 qua ResultHandler."""
    expected_type = ACTION_OUTPUT_ARTIFACT_TYPES.get(action, ArtifactType.DOCUMENTATION)
    art_id = f"art-{task_id}"
    art_uri = f"artifact://{task_id}-{action}.out"

    artifact = ArtifactRef(
        artifact_id=art_id,
        name=f"{action}_output.dat",
        type=expected_type,
        version="1.0",
        uri=art_uri,
        checksum="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        created_by=agent_id,
        created_at=datetime.now(timezone.utc),
    )

    meta: Dict[str, Any] = {}
    if action in ["run_test_suite", "security_review"]:
        meta["verdict"] = "PASS"
        meta["findings"] = []

    res = TaskResult(
        task_id=task_id,
        agent_id=agent_id,
        status=TaskStatus.COMPLETED,
        summary=f"Hoàn thành xuất sắc action '{action}' cho Todo App.",
        artifacts=[artifact],
        outputs=[{"action": action, "status": "SUCCESS"}],
        metadata=meta,
    )

    with session_factory() as session:
        # Ghi artifact vào bảng artifacts để bảo đảm tính truy vết
        db_art = ArtifactModel(
            artifact_id=art_id,
            run_id=run_id,
            task_id=task_id,
            name=f"{action}_output.dat",
            type=expected_type.value,
            version="1.0",
            uri=art_uri,
            checksum="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            created_by=agent_id,
            created_at=utc_now(),
        )
        session.add(db_art)
        session.commit()

        worker_principal = WorkerPrincipal(
            worker_id=worker_id,
            machine_id=machine_id,
            allowed_agents=[agent_id],
        )

        ResultHandler.handle_result(
            session=session,
            task_id=task_id,
            result=res,
            worker=worker_principal,
            publisher=publisher,
        )


def setup_all_workers_and_agents(session_factory):
    """Khởi tạo đầy đủ 5 máy và 10 agent theo bảng đăng ký mục 3."""
    now = utc_now()
    workers = [
        ("worker-pc1", "PC1", "http://192.168.1.101:8000"),
        ("worker-pc2", "PC2", "http://192.168.1.102:8000"),
        ("worker-pc3", "PC3", "http://192.168.1.103:8000"),
        ("worker-pc4", "PC4", "http://192.168.1.104:8000"),
        ("worker-pc5", "PC5", "http://192.168.1.105:8000"),
    ]
    agents = [
        ("pm-01", "PM", "PC1", "LocalWorker", ["project_planning", "scope_management"]),
        ("devops-01", "DEVOPS", "PC1", "LocalWorker", ["docker", "ci_cd", "deployment"]),
        ("sa-01", "SA", "PC2", "Worker Host", ["system_architecture", "tech_stack_selection"]),
        ("dba-01", "DBA", "PC2", "Worker Host", ["schema_design", "query_optimization"]),
        ("ba-01", "BA", "PC3", "Worker Host", ["requirement_analysis", "user_story"]),
        ("po-01", "PO", "PC3", "Worker Host", ["backlog_management", "acceptance"]),
        ("dev-01", "DEV", "PC4", "Worker Host", ["coding", "bug_fixing"]),
        ("tester-01", "TESTER", "PC4", "Worker Host", ["test_case_generation", "automated_testing"]),
        ("scrum_master-01", "SCRUM_MASTER", "PC5", "Worker Host", ["sprint_management", "retrospective"]),
        ("security-01", "SECURITY", "PC5", "Worker Host", ["security_review", "vulnerability_scan"]),
    ]

    with session_factory() as db:
        for wid, mid, url in workers:
            w = db.get(WorkerModel, wid)
            if not w:
                w = WorkerModel(
                    worker_id=wid,
                    machine_id=mid,
                    base_url=url,
                    status="ONLINE",
                    contract_version="1.3.0",
                    worker_version="1.0.0",
                    last_heartbeat_at=now,
                    registered_at=now,
                    updated_at=now,
                )
                db.add(w)
            else:
                w.status = "ONLINE"
                w.last_heartbeat_at = now

        for aid, role, machine, runner, caps in agents:
            a = db.get(AgentModel, aid)
            if not a:
                a = AgentModel(
                    agent_id=aid,
                    role=role,
                    machine=machine,
                    runner=runner,
                    status="IDLE",
                    current_task=None,
                    capabilities=caps,
                    updated_at=now,
                )
                db.add(a)
            else:
                a.status = "IDLE"
                a.current_task = None
                a.machine = machine
                a.role = role

        db.commit()


def test_full_delivery_pipeline_e2e_and_check_process():
    """Chạy toàn trình một WorkflowRun mẫu từ prompt tới COMPLETED và xác minh 100% check_process PASS."""
    session_factory = get_session_factory()
    fake_redis = fakeredis.FakeRedis(decode_responses=True)
    publisher = EventPublisher(fake_redis)

    # 1. Đảm bảo hạ tầng Worker và Agent sẵn sàng
    setup_all_workers_and_agents(session_factory)

    run_id = f"RUN-E2E-{uuid.uuid4().hex[:6].upper()}"
    project_id = "PRJ-TODO-APP"

    # Dọn dẹp các run active cũ để không vướng concurrency limit
    with session_factory() as sess:
        sess.query(WorkflowRunModel).filter(
            WorkflowRunModel.status.in_([RunStatus.PLANNING.value, RunStatus.RUNNING.value, RunStatus.AWAITING_APPROVAL.value])
        ).update({"status": RunStatus.COMPLETED.value})
        sess.commit()

        # 2. Khởi tạo WorkflowRun trên PC1
        WorkflowRunRepository.create_run(
            sess,
            {
                "run_id": run_id,
                "project_id": project_id,
                "brief": "Xây dựng ứng dụng Todo List một trang bằng FastAPI và SQLite.",
                "status": RunStatus.RUNNING.value,
            },
        )
        AuditService.log_run_status(sess, run_id=run_id, old_status="CREATED", new_status="RUNNING", reason="Start E2E run")
        sess.commit()

    # 3. Định nghĩa toàn bộ pipeline DAG theo process/software_delivery.yaml
    pipeline_spec = [
        # Scoping
        ("t1_po_backlog", "PO", "define_backlog", "po-01", "worker-pc3", "PC3", []),
        ("t2_ba_srs", "BA", "write_requirements", "ba-01", "worker-pc3", "PC3", ["t1_po_backlog"]),
        ("t3_ba_stories", "BA", "write_user_stories", "ba-01", "worker-pc3", "PC3", ["t2_ba_srs"]),
        # Design (phụ thuộc Gate APPROVE_SCOPE sau t3)
        ("t4_sa_arch", "SA", "design_architecture", "sa-01", "worker-pc2", "PC2", ["t3_ba_stories"]),
        ("t5_dba_schema", "DBA", "design_schema", "dba-01", "worker-pc2", "PC2", ["t4_sa_arch"]),
        # Foundation & Sprint
        ("t6_tester_cases", "TESTER", "create_test_cases", "tester-01", "worker-pc4", "PC4", ["t5_dba_schema"]),
        ("t7_dev_skeleton", "DEV", "implement_project", "dev-01", "worker-pc4", "PC4", ["t6_tester_cases"]),
        ("t8_dev_feature", "DEV", "implement_feature", "dev-01", "worker-pc4", "PC4", ["t7_dev_skeleton"]),
        ("t9_tester_suite", "TESTER", "run_test_suite", "tester-01", "worker-pc4", "PC4", ["t8_dev_feature"]),
        ("t10_scrum_review", "SCRUM_MASTER", "run_sprint_review", "scrum_master-01", "worker-pc5", "PC5", ["t9_tester_suite"]),
        # Verification
        ("t11_sec_review", "SECURITY", "security_review", "security-01", "worker-pc5", "PC5", ["t10_scrum_review"]),
        # Staging Deployment
        ("t12_ci_config", "DEVOPS", "generate_ci_config", "devops-01", "worker-pc1", "PC1", ["t11_sec_review"]),
        ("t13_build_img", "DEVOPS", "build_image", "devops-01", "worker-pc1", "PC1", ["t12_ci_config"]),
        ("t14_deploy_stg", "DEVOPS", "deploy", "devops-01", "worker-pc1", "PC1", ["t13_build_img"]),
        ("t15_health_chk", "DEVOPS", "health_check", "devops-01", "worker-pc1", "PC1", ["t14_deploy_stg"]),
        # Closure
        ("t16_po_accept", "PO", "accept_release", "po-01", "worker-pc3", "PC3", ["t15_health_chk"]),
        ("t17_scrum_retro", "SCRUM_MASTER", "run_retrospective", "scrum_master-01", "worker-pc5", "PC5", ["t16_po_accept"]),
        ("t18_pm_report", "PM", "report_status", "pm-01", "worker-pc1", "PC1", ["t17_scrum_retro"]),
    ]

    tasks_data = [
        {
            "task_id": f"{run_id}_{tid}",
            "role": role,
            "action": action,
            "agent_id": agent_id,
            "instruction": f"Execute {action} for {project_id}",
        }
        for tid, role, action, agent_id, _, _, _ in pipeline_spec
    ]

    dependencies = [
        (f"{run_id}_{tid}", f"{run_id}_{pid}")
        for tid, _, _, _, _, _, prereqs in pipeline_spec
        for pid in prereqs
    ]

    with session_factory() as sess:
        TaskManager.create_tasks_from_plan(
            session=sess,
            run_id=run_id,
            tasks_data=tasks_data,
            dependencies=dependencies,
        )
        sess.commit()

    # 4. Khởi tạo Dispatcher và Mock Worker Clients
    dispatcher = DispatcherService(
        session_factory=session_factory,
        redis_client=fake_redis,
        publisher=publisher,
    )
    mock_client = E2EMockWorkerClient()
    for wid in ("worker-pc1", "worker-pc2", "worker-pc3", "worker-pc4", "worker-pc5"):
        dispatcher.register_worker_client(wid, mock_client)

    engine = OrchestratorEngine(
        session_factory=session_factory,
        publisher=publisher,
        dispatcher=dispatcher,
        checkpoint_store=MemoryCheckpointStore(),
    )

    # 5. Thực thi tuần tự qua từng bước pipeline
    for i, (tid_short, role_str, action_name, agent_id, wid, mid, _) in enumerate(pipeline_spec):
        full_tid = f"{run_id}_{tid_short}"

        # Nếu là bước Design (t4), kiểm tra Gate APPROVE_SCOPE
        if tid_short == "t4_sa_arch":
            with session_factory() as sess:
                ApprovalRepository.record_decision(
                    session=sess,
                    run_id=run_id,
                    gate="APPROVE_SCOPE",
                    status="APPROVED",
                    decided_by="ADMIN",
                    comment="Scope approved by Product Committee",
                )
                AuditService.log_approval(sess, run_id=run_id, gate="APPROVE_SCOPE", status="APPROVED", actor="ADMIN")
                sess.commit()

        # Step run để scheduler tính READY và dispatch task
        engine.step_run(run_id)

        # Worker gửi kết quả hoàn tất về PC1
        submit_completed_result(
            session_factory=session_factory,
            publisher=publisher,
            task_id=full_tid,
            agent_id=agent_id,
            action=action_name,
            role=AgentRole(role_str),
            worker_id=wid,
            machine_id=mid,
            run_id=run_id,
        )

    # 6. Đánh dấu Run hoàn thành
    with session_factory() as sess:
        run = sess.get(WorkflowRunModel, run_id)
        run.status = RunStatus.COMPLETED.value
        run.completed_at = utc_now()
        AuditService.log_run_status(sess, run_id=run_id, old_status="RUNNING", new_status="COMPLETED", reason="All 18 pipeline tasks succeeded.")
        sess.commit()

    # 7. Thẩm định quy trình bằng check_process
    with session_factory() as sess:
        checks, all_passed = verify_run(sess, run_id)
        report_file = generate_report_file(run_id, checks, all_passed)

    print(f"\n[+] E2E Run '{run_id}' hoàn tất: check_process all_passed={all_passed}")
    print(f"[+] Báo cáo thẩm định: {report_file}")

    assert all_passed is True
    assert len(checks) == 10
    for c in checks:
        assert c["passed"] is True, f"Tiêu chí quy trình thất bại: {c['name']} - {c['details']}"
    assert report_file.exists()


if __name__ == "__main__":
    test_full_delivery_pipeline_e2e_and_check_process()
    print("[+] Toàn bộ kiểm thử E2E Delivery Pipeline và check_process ĐẠT 100% PASS.")
