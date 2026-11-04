"""test_plan_service.py - Kiểm thử toàn diện dịch vụ Planner, Workflow Engine và PlanValidator (Prompt S8).

Bao gồm các kịch bản:
1. Kế hoạch hợp lệ từ phần mềm chuẩn vượt qua kiểm định.
2. Vai trò (role) hoặc hành động (action) không hợp lệ bị từ chối với lỗi rõ ràng.
3. Phát hiện chu trình phụ thuộc (Cycle / DAG violation).
4. Phát hiện tham chiếu task không tồn tại (Dangling reference).
5. Vượt quá giới hạn số lượng task tối đa (MAX_TASKS_PER_RUN).
6. Thiếu bước kiểm thử hoặc bảo mật, hoặc đảo lộn thứ tự bị từ chối.
7. Vòng lặp thử lại tối đa 2 lần khi LLM lỗi JSON / plan hỏng, và fallback an toàn sang kế hoạch mẫu.
8. Hỗ trợ prompt song ngữ (Tiếng Việt & Tiếng Anh).
9. Mở rộng phần động User Stories (expand_stories): giới hạn tối đa 8 stories, chia sprint 3 stories,
   sinh test suite & sprint review, nối lại phụ thuộc security_review vào sprint review cuối.
10. Chế độ duyệt APPROVAL_MODE=True chặn run ở AWAITING_APPROVAL, ngăn Dispatcher giao task
    cho tới khi được phê duyệt qua API/Service.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import fakeredis
import pytest
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, ArtifactType
from agent_contract.system_models import DispatchState, RunStatus
from control.app.config import settings
from control.app.database.models import TaskDependencyModel, TaskModel, WorkflowRunModel
from control.app.database.repositories import TaskRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.queue.events import EventPublisher
from control.app.services.approval import ApprovalService
from control.app.services.dispatcher import DispatcherService
from control.app.services.plan_service import (
    MAX_STORIES_DEFAULT,
    SPRINT_SIZE_DEFAULT,
    PlanService,
    build_default_plan,
)
from control.app.services.plan_validator import PlanValidator, PlanValidationResult


@pytest.fixture
def session_factory():
    return get_session_factory()


@pytest.fixture
def fake_redis_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture
def publisher(fake_redis_client):
    return EventPublisher(fake_redis_client)


class TestPlanValidator:
    """Kiểm tra các quy tắc kiểm định tất định của PlanValidator."""

    def test_1_valid_default_plan_passes(self):
        """Kế hoạch chuẩn mực từ software_delivery.yaml phải 100% hợp lệ."""
        plan = build_default_plan(brief="Xây dựng sàn thương mại điện tử nông sản", project_id="PRJ-AGRI-01")
        validator = PlanValidator()
        result = validator.validate(plan)

        assert result.valid is True
        assert len(result.errors) == 0
        assert len(result.topological_order) == len(plan["tasks"])

    def test_2_invalid_role_or_action_rejected(self):
        """Task có vai trò không tồn tại hoặc action không thuộc vai trò bị phát hiện ngay."""
        plan = build_default_plan(brief="Test invalid", project_id="PRJ-INV")
        # Gán role bịa đặt
        plan["tasks"][0]["role"] = "HACKER"
        validator = PlanValidator()
        res1 = validator.validate(plan)
        assert res1.valid is False
        assert any("vai trò 'HACKER' không hợp lệ" in e for e in res1.errors)

        # Gán action không được phép cho PO (PO không được làm deploy)
        plan["tasks"][0]["role"] = "PO"
        plan["tasks"][0]["action"] = "deploy"
        res2 = validator.validate(plan)
        assert res2.valid is False
        assert any("Action 'deploy' không được hỗ trợ cho vai trò 'PO'" in e for e in res2.errors)

    def test_3_cyclic_dependency_detected(self):
        """Chu trình phụ thuộc giữa các tasks (A -> B -> C -> A) phải bị phát hiện và báo lỗi."""
        plan = build_default_plan(brief="Test Cycle", project_id="PRJ-CYC")
        # Tạo chu trình: po_define_backlog phụ thuộc vào devops_health_check (vốn ở cuối pipeline)
        # Trong khi devops_health_check đã phụ thuộc chuỗi từ po_define_backlog
        plan["tasks"][0]["depends_on"] = ["devops_health_check"]

        validator = PlanValidator()
        res = validator.validate(plan)
        assert res.valid is False
        assert any("Phát hiện chu trình phụ thuộc trong kế hoạch" in e for e in res.errors)

    def test_4_dangling_reference_detected(self):
        """Tham chiếu tới task_id không tồn tại trong kế hoạch bị báo lỗi cụ thể."""
        plan = build_default_plan(brief="Test Dangling", project_id="PRJ-DANG")
        plan["tasks"][1]["depends_on"].append("non_existent_ghost_task")

        validator = PlanValidator()
        res = validator.validate(plan)
        assert res.valid is False
        assert any("Dangling reference" in e for e in res.errors)
        assert any("non_existent_ghost_task" in e for e in res.errors)

    def test_5_max_tasks_per_run_exceeded(self):
        """Số lượng task vượt quá giới hạn cấu hình bị từ chối."""
        plan = build_default_plan(brief="Test Limit", project_id="PRJ-LIM")
        validator = PlanValidator(max_tasks=5)  # Giới hạn nhỏ 5 task
        res = validator.validate(plan)

        assert res.valid is False
        assert any("vượt quá giới hạn tối đa" in e for e in res.errors)

    def test_6_missing_mandatory_testing_or_security_step_fails(self):
        """Bỏ qua bước kiểm thử hoặc bảo mật bị từ chối nghiêm ngặt."""
        # 1. Bỏ qua bước kiểm thử (TESTER.create_test_cases)
        plan_no_test = build_default_plan(brief="No test", project_id="PRJ-NO-TEST")
        plan_no_test["tasks"] = [t for t in plan_no_test["tasks"] if t["action"] != "create_test_cases"]
        # Sửa lại phụ thuộc của bước kế tiếp
        for t in plan_no_test["tasks"]:
            if "tester_create_test_cases" in t.get("depends_on", []):
                t["depends_on"].remove("tester_create_test_cases")

        validator = PlanValidator()
        res_test = validator.validate(plan_no_test)
        assert res_test.valid is False
        assert any("TESTER.create_test_cases" in e for e in res_test.errors)

        # 2. Bỏ qua bước bảo mật (SECURITY.security_review)
        plan_no_sec = build_default_plan(brief="No sec", project_id="PRJ-NO-SEC")
        plan_no_sec["tasks"] = [t for t in plan_no_sec["tasks"] if t["action"] != "security_review"]
        for t in plan_no_sec["tasks"]:
            if "security_security_review" in t.get("depends_on", []):
                t["depends_on"].remove("security_security_review")

        res_sec = validator.validate(plan_no_sec)
        assert res_sec.valid is False
        assert any("SECURITY.security_review" in e for e in res_sec.errors)


class TestPlanServiceExecution:
    """Kiểm tra hoạt động điều phối sinh kế hoạch, retry, fallback và mở rộng story."""

    def test_7_retry_loop_and_fallback_on_pm_failure(self):
        """PM Agent lỗi JSON / trả kế hoạch sai sẽ được thử lại 2 lần (tổng 3 lần) và fallback kế hoạch mẫu."""
        call_count = 0
        received_errors: List[Optional[List[str]]] = []

        def faulty_pm_caller(prompt: str, project_id: str, errors: Optional[List[str]] = None) -> Dict[str, Any]:
            nonlocal call_count
            call_count += 1
            received_errors.append(errors)
            # Luôn trả về kế hoạch không hợp lệ (thiếu testing và security)
            return {
                "project_name": project_id,
                "overview": prompt,
                "tasks": [
                    {
                        "task_id": "dummy_task",
                        "role": "PO",
                        "action": "define_backlog",
                        "instruction": "Dummy",
                        "acceptance_criteria": ["Done"],
                        "depends_on": [],
                    }
                ],
            }

        service = PlanService()
        plan = service.generate_plan_with_retry(
            prompt="Ứng dụng đặt đồ ăn trực tuyến",
            project_id="PRJ-FOOD",
            pm_caller=faulty_pm_caller,
        )

        # Đã gọi đúng 3 lần (lần đầu + 2 lần retry)
        assert call_count == 3
        # Lần 2 và 3 nhận được danh sách lỗi từ lần trước
        assert received_errors[0] is None
        assert received_errors[1] is not None
        assert len(received_errors[1]) > 0
        assert received_errors[2] is not None

        # Kết quả cuối cùng là kế hoạch mẫu hợp lệ (fallback thành công, không làm hỏng luồng)
        assert len(plan["tasks"]) >= 13
        assert any(t["action"] == "create_test_cases" for t in plan["tasks"])
        assert any(t["action"] == "security_review" for t in plan["tasks"])

    def test_8_bilingual_prompts_support(self):
        """Hỗ trợ prompt cả Tiếng Việt và Tiếng Anh hoàn hảo."""
        service = PlanService()

        # Prompt Tiếng Việt
        vn_plan = service.generate_plan_with_retry(
            prompt="Hệ thống quản lý chuỗi cung ứng thủy hải sản xuất khẩu",
            project_id="PRJ-VN-01",
        )
        assert vn_plan["project_name"] == "PRJ-VN-01"
        assert len(vn_plan["tasks"]) > 0

        # Prompt Tiếng Anh
        en_plan = service.generate_plan_with_retry(
            prompt="Microservices-based cloud IoT fleet management platform",
            project_id="PRJ-EN-01",
        )
        assert en_plan["project_name"] == "PRJ-EN-01"
        assert len(en_plan["tasks"]) > 0

    def test_9_expand_stories_limits_and_sprints(self, session_factory):
        """expand_stories: tối đa 8 stories, chia sprint 3 stories, nối lại phụ thuộc security_review."""
        run_id = f"RUN-STORIES-{uuid.uuid4().hex[:6]}"
        with session_factory() as session:
            # Tạo run ban đầu với kế hoạch chuẩn
            WorkflowRunRepository.create_run(
                session,
                {"run_id": run_id, "project_id": "PRJ-STORIES", "brief": "Story test", "status": "RUNNING"},
            )
            default_plan = build_default_plan(brief="Story test", project_id="PRJ-STORIES")
            service = PlanService()
            service.save_plan_to_db(session, run_id=run_id, plan=default_plan)

            # Tạo danh sách 10 User Stories (vượt quá giới hạn 8)
            input_stories = [
                {
                    "id": f"US-{i:02d}",
                    "title": f"Tính năng số {i}",
                    "acceptance_criteria": [f"Tiêu chí {i}.1", f"Tiêu chí {i}.2"],
                }
                for i in range(1, 11)
            ]

            expanded_tasks = service.expand_stories(
                session=session,
                run_id=run_id,
                stories_artifact_or_data=input_stories,
                max_stories=MAX_STORIES_DEFAULT,  # 8
                sprint_size=SPRINT_SIZE_DEFAULT,  # 3
            )

            # 1. Kiểm tra số lượng stories được xử lý:
            # 8 stories chia thành:
            # Sprint 1: US-01, US-02, US-03 -> 3 DEV + 1 TESTER + 1 SCRUM_MASTER = 5 tasks
            # Sprint 2: US-04, US-05, US-06 -> 3 DEV + 1 TESTER + 1 SCRUM_MASTER = 5 tasks
            # Sprint 3: US-07, US-08         -> 2 DEV + 1 TESTER + 1 SCRUM_MASTER = 4 tasks
            # Tổng cộng = 14 tasks động được tạo
            dev_tasks = [t for t in expanded_tasks if t.action == "implement_feature"]
            test_tasks = [t for t in expanded_tasks if t.action == "run_test_suite"]
            sm_tasks = [t for t in expanded_tasks if t.action == "run_sprint_review"]

            assert len(dev_tasks) == 8  # Enforce MAX_STORIES=8
            assert len(test_tasks) == 3  # 3 Sprints
            assert len(sm_tasks) == 3   # 3 Sprints
            assert len(expanded_tasks) == 14

            # 2. Kiểm tra tính tuần tự trong Sprint 1:
            # US-01 phụ thuộc dev_implement_project
            # US-02 phụ thuộc US-01
            # US-03 phụ thuộc US-02
            # tester_run_test_suite_sprint_1 phụ thuộc US-03
            # scrum_master_run_sprint_review_sprint_1 phụ thuộc tester
            dep_query = session.query(TaskDependencyModel).all()
            dep_map = [(d.task_id, d.depends_on_task_id) for d in dep_query]

            assert (f"{run_id}_dev_feature_us_01", f"{run_id}_dev_implement_project") in dep_map
            assert (f"{run_id}_dev_feature_us_02", f"{run_id}_dev_feature_us_01") in dep_map
            assert (f"{run_id}_dev_feature_us_03", f"{run_id}_dev_feature_us_02") in dep_map
            assert (f"{run_id}_tester_run_test_suite_sprint_1", f"{run_id}_dev_feature_us_03") in dep_map
            assert (f"{run_id}_scrum_master_run_sprint_review_sprint_1", f"{run_id}_tester_run_test_suite_sprint_1") in dep_map

            # Sprint 2 phụ thuộc vào review của Sprint 1
            assert (f"{run_id}_dev_feature_us_04", f"{run_id}_scrum_master_run_sprint_review_sprint_1") in dep_map

            # 3. Kiểm tra nối lại phụ thuộc của security_review vào sprint review cuối (Sprint 3)
            # Không còn phụ thuộc dev_implement_project
            assert (f"{run_id}_security_security_review", f"{run_id}_dev_implement_project") not in dep_map
            assert (f"{run_id}_security_security_review", f"{run_id}_scrum_master_run_sprint_review_sprint_3") in dep_map

    def test_10_approval_mode_blocks_dispatch_until_approved(self, session_factory, fake_redis_client, publisher):
        """APPROVAL_MODE=True chặn run ở AWAITING_APPROVAL, ngăn Dispatcher giao việc cho đến khi được duyệt."""
        run_id = f"RUN-APPR-{uuid.uuid4().hex[:6]}"
        with session_factory() as session:
            WorkflowRunRepository.create_run(
                session,
                {"run_id": run_id, "project_id": "PRJ-APPR", "brief": "Approval test", "status": "PLANNING"},
            )

            service = PlanService(publisher=publisher)
            # Dựng và lưu kế hoạch với APPROVAL_MODE bật
            plan = build_default_plan(brief="Approval test", project_id="PRJ-APPR")
            service.save_plan_to_db(session, run_id=run_id, plan=plan)

            run = WorkflowRunRepository.get_run(session, run_id)
            # Run phải ở AWAITING_APPROVAL
            assert run.status == RunStatus.AWAITING_APPROVAL.value

        # Thử chạy một chu kỳ Dispatcher
        dispatcher = DispatcherService(
            session_factory=session_factory,
            redis_client=fake_redis_client,
            publisher=publisher,
        )
        dispatched_count = dispatcher.run_dispatch_cycle(run_id=run_id)
        # Không có task nào được giao vì run đang AWAITING_APPROVAL
        assert dispatched_count == 0

        # Kiểm tra task đầu tiên vẫn ở trạng thái QUEUED
        with session_factory() as session:
            task1 = session.query(TaskModel).filter(
                TaskModel.run_id == run_id,
                TaskModel.action == "define_backlog",
            ).first()
            assert task1 is not None
            assert task1.dispatch_state == DispatchState.QUEUED.value

            # Thực hiện Phê duyệt (Approval)
            appr_svc = ApprovalService(publisher=publisher)
            res = appr_svc.approve(session, run_id=run_id, gate="APPROVE_PLAN", decided_by="TEST_ADMIN")
            assert res["status"] == "approved"
            assert res["run_status"] == RunStatus.RUNNING.value

            refreshed_run = WorkflowRunRepository.get_run(session, run_id)
            assert refreshed_run.status == RunStatus.RUNNING.value
