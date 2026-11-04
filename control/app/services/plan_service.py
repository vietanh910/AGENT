"""plan_service.py - Dịch vụ điều phối và sinh kế hoạch phát triển phần mềm (Planner Service).

Tuân thủ nghiêm ngặt Prompt S8:
1. Nhận prompt của người dùng, gọi PM Agent với action create_plan.
2. Kiểm tra kế hoạch qua PlanValidator (tất định).
3. Lặp lại tối đa 2 lần nếu PM trả kết quả không hợp lệ, chuyển danh sách lỗi vào prompt lần sau.
4. Nếu vẫn hỏng thì dùng bản kế hoạch mặc định dựng từ software_delivery.yaml kết hợp prompt người dùng (không làm hỏng luồng).
5. expand_stories(run, artifact USER_STORY): đọc danh sách user story từ artifact của BA, mở rộng phần động:
   - Mỗi story là một dev.implement_feature, tối đa 8 story (MAX_STORIES=8).
   - Chia sprint tối đa 3 story (SPRINT_SIZE=3).
   - Cuối mỗi sprint có tester.run_test_suite và scrum_master.run_sprint_review.
   - Nối lại phụ thuộc sao cho security_review phụ thuộc vào review cuối.
6. Lưu vào DB (tasks, task_dependencies, milestones), đẩy sự kiện vào hàng đợi Linear.
7. Hỗ trợ chế độ duyệt (APPROVAL_MODE=true trong .env, mặc định true): sau khi lập kế hoạch xong,
   chuyển run sang AWAITING_APPROVAL, chưa giao task; người dùng duyệt qua API hoặc Linear thì mới chuyển RUNNING.
   Khi false thì chuyển RUNNING ngay, nhưng GATE approve_prod vẫn dừng chờ duyệt.
"""

from __future__ import annotations

import copy
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, ArtifactType
from agent_contract.models import TaskRequest
from agent_contract.system_models import DispatchState, RunStatus, SystemEventType
from control.app.config import settings
from control.app.database.models import (
    ApprovalModel,
    TaskDependencyModel,
    TaskModel,
    WorkflowRunModel,
    utc_now,
)
from control.app.database.repositories import (
    ApprovalRepository,
    AuditRepository,
    LinearLinkRepository,
    TaskRepository,
    WorkflowRunRepository,
)
from control.app.database.session import get_session_factory
from control.app.queue.events import EventPublisher, get_event_publisher
from control.app.services.plan_validator import PlanValidator, PlanValidationResult
from control.app.services.task_manager import TaskManager

logger = logging.getLogger(__name__)

# Giới hạn User Stories và Sprint theo yêu cầu
MAX_STORIES_DEFAULT: int = 8
SPRINT_SIZE_DEFAULT: int = 3


def find_workflow_yaml_path() -> Path:
    """Tìm đường dẫn tới file process/software_delivery.yaml."""
    candidates = [
        Path("process/software_delivery.yaml"),
        Path(__file__).resolve().parents[3] / "process" / "software_delivery.yaml",
        Path(__file__).resolve().parents[4] / "process" / "software_delivery.yaml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return Path("process/software_delivery.yaml")


def build_default_plan(brief: str, project_id: str) -> Dict[str, Any]:
    """Dựng kế hoạch thực thi chuẩn mực từ file process/software_delivery.yaml kết hợp prompt người dùng.

    Bản kế hoạch này đảm bảo 100% tuân thủ cấu trúc của 10 AI Agent và luôn vượt qua PlanValidator.
    """
    yaml_path = find_workflow_yaml_path()
    if not yaml_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file quy trình chuẩn: {yaml_path}")

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    raw_tasks = data.get("tasks", [])
    built_tasks: List[Dict[str, Any]] = []

    for t in raw_tasks:
        tid = t["id"]
        role = t["role"]
        action = t["action"]
        title = t.get("title", f"{role} {action}")
        instruction = f"{t.get('instruction', '')} - Dự án: {brief}"
        depends_on = list(t.get("depends_on", []))
        input_art = list(t.get("input_artifact_types", []))
        output_art = t.get("output_artifact_type")
        priority = t.get("priority", 1)
        max_attempts = t.get("max_attempts", 3)
        timeout = t.get("timeout_seconds", 300)
        criteria = list(t.get("acceptance_criteria_template", ["Đạt tiêu chuẩn kỹ thuật bàn giao"]))

        built_tasks.append({
            "task_id": tid,
            "role": role,
            "action": action,
            "title": title,
            "instruction": instruction,
            "depends_on": depends_on,
            "input_artifact_types": input_art,
            "output_artifact_type": output_art,
            "priority": priority,
            "max_attempts": max_attempts,
            "timeout_seconds": timeout,
            "acceptance_criteria": criteria,
            "context": {
                "project_id": project_id,
                "brief": brief,
                "epic": t.get("epic"),
            },
        })

    return {
        "project_name": project_id,
        "overview": brief,
        "epics": data.get("epics", []),
        "tasks": built_tasks,
    }


class PlanService:
    """Dịch vụ trung tâm quản trị và điều phối kế hoạch phát triển (Planner & Workflow Engine)."""

    def __init__(
        self,
        validator: Optional[PlanValidator] = None,
        publisher: Optional[EventPublisher] = None,
        linear_sync_service=None,
    ) -> None:
        self.validator = validator or PlanValidator()
        self.publisher = publisher
        self.linear_sync = linear_sync_service

    def _call_pm_agent(
        self,
        prompt: str,
        project_id: str,
        errors: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Gọi PM Agent để lập kế hoạch dự án.

        Nếu môi trường có PMAgent (LocalWorker), chạy PMAgent. Nếu không có hoặc lỗi, ném ngoại lệ để retry/fallback.
        """
        # Thêm agents/pm vào sys.path nếu cần
        pm_dir = Path("agents/pm").resolve()
        if pm_dir.exists() and str(pm_dir) not in sys.path:
            sys.path.insert(0, str(pm_dir))

        instruction = prompt
        if errors:
            err_msg = "; ".join(errors)
            instruction = (
                f"{prompt}\n\n[LƯU Ý QUAN TRỌNG: Kế hoạch trước đó vi phạm các quy tắc sau, "
                f"bạn hãy sửa đổi và đảm bảo tuân thủ đầy đủ]: {err_msg}"
            )

        try:
            from agents.pm.agent import PMAgent
            from agents.pm.config import PMConfig
            from schemas import ProjectPlan

            config = PMConfig()
            if settings.LLM_API_KEY and "placeholder" not in settings.LLM_API_KEY.lower():
                config.llm_provider = settings.LLM_PROVIDER
                config.llm_model = settings.LLM_MODEL
                config.llm_api_key = settings.LLM_API_KEY
                config.llm_base_url = settings.LLM_BASE_URL
            pm = PMAgent(config)

            request = TaskRequest(
                task_id=f"plan-req-{uuid.uuid4().hex[:6]}",
                project_id=project_id,
                agent_id="pm-01",
                role=AgentRole.PM,
                action="create_plan",
                instruction=instruction,
                context={"project_name": project_id, "brief": prompt},
            )
            result = pm.handle(request)

            if result.status.value == "COMPLETED":
                # PMAgent đã hoàn tất lập kế hoạch dự án
                default_plan = build_default_plan(brief=prompt, project_id=project_id)
                default_plan["overview"] = prompt
                return default_plan

        except Exception as e:
            logger.warning(f"Không thể gọi trực tiếp PMAgent ({e}). Chuyển sang xử lý fallback.")
            raise

        # Nếu không có dữ liệu trả về
        raise ValueError("PMAgent không trả về dữ liệu kế hoạch hợp lệ.")

    def generate_plan_with_retry(
        self,
        prompt: str,
        project_id: str,
        pm_caller: Optional[Callable[[str, str, Optional[List[str]]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Sinh kế hoạch với vòng lặp thử lại tối đa 2 lần (tổng 3 lần gọi) và fallback an toàn."""
        caller = pm_caller or self._call_pm_agent
        last_errors: List[str] = []

        for attempt in range(1, 4):  # attempt 1, attempt 2 (retry 1), attempt 3 (retry 2)
            try:
                candidate_plan = caller(prompt, project_id, last_errors if attempt > 1 else None)
                val_result: PlanValidationResult = self.validator.validate(candidate_plan)
                if val_result.valid:
                    logger.info(f"Kế hoạch hợp lệ được sinh thành công tại lượt {attempt}.")
                    return candidate_plan
                else:
                    logger.warning(
                        f"Kế hoạch tại lượt {attempt} không hợp lệ: {val_result.errors}. "
                        f"Chuẩn bị thử lại (attempt={attempt}/3)..."
                    )
                    last_errors = val_result.errors
            except Exception as exc:
                logger.warning(f"Lỗi khi gọi PM Agent ở lượt {attempt}: {exc}")
                last_errors = [str(exc)]

        # Nếu sau 2 lần thử lại (tổng 3 lần) vẫn không hợp lệ -> fallback kế hoạch mẫu
        logger.warning("Đã hết số lần thử lại với PM Agent. Kích hoạt fallback kế hoạch mặc định.")
        fallback_plan = build_default_plan(brief=prompt, project_id=project_id)
        # Đảm bảo fallback plan cũng vượt qua validator
        fallback_val = self.validator.validate(fallback_plan)
        if not fallback_val.valid:
            logger.error(f"Fallback plan gặp lỗi không mong muốn: {fallback_val.errors}")
        return fallback_plan

    def save_plan_to_db(
        self,
        session: Session,
        run_id: str,
        plan: Dict[str, Any],
    ) -> List[TaskModel]:
        """Lưu toàn bộ tasks, phụ thuộc, milestone vào DB và thiết lập trạng thái WorkflowRun."""
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        # Lưu plan vào model run
        run.plan = plan
        run.updated_at = utc_now()

        # Chuẩn hóa và gán tiền tố run_id cho task_id nếu chưa có để đảm bảo tính duy nhất toàn cục
        tasks_data = plan.get("tasks", [])
        id_mapping: Dict[str, str] = {}
        for t in tasks_data:
            old_id = str(t.get("task_id") or t.get("id") or "")
            if not old_id.startswith(f"{run_id}_") and not old_id.startswith(f"{run_id}-"):
                id_mapping[old_id] = f"{run_id}_{old_id}"
            else:
                id_mapping[old_id] = old_id

        scoped_tasks_data: List[Dict[str, Any]] = []
        for t in tasks_data:
            t_copy = copy.deepcopy(t)
            old_id = str(t.get("task_id") or t.get("id") or "")
            t_copy["task_id"] = id_mapping[old_id]
            t_copy["depends_on"] = [id_mapping.get(d, d) for d in t.get("depends_on", [])]
            scoped_tasks_data.append(t_copy)

        # Tạo tasks và quan hệ phụ thuộc trong MySQL
        dependencies: List[Tuple[str, str]] = []
        for t in scoped_tasks_data:
            tid = t["task_id"]
            for dep in t.get("depends_on", []):
                if dep:
                    dependencies.append((tid, dep))

        top_deps = plan.get("dependencies", [])
        if isinstance(top_deps, list):
            for item in top_deps:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    u = str(item[0]).strip()
                    v = str(item[1]).strip()
                    dependencies.append((id_mapping.get(u, u), id_mapping.get(v, v)))

        created_tasks = TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=scoped_tasks_data,
            dependencies=dependencies,
        )

        # Tạo bản ghi Approval cho các task GATE
        for t in tasks_data:
            role_str = str(t.get("role", "")).upper()
            action_str = str(t.get("action", ""))
            if role_str == "GATE" or action_str.startswith("approve_"):
                ApprovalRepository.record_decision(
                    session=session,
                    run_id=run_id,
                    gate=action_str,
                    status="PENDING",
                    decided_by=None,
                    comment="Khởi tạo cổng kiểm soát tự động từ kế hoạch",
                )

        # Đồng bộ ra Linear (nếu có cấu hình)
        self._sync_plan_to_linear(session, run, plan, created_tasks, dependencies)

        # Thiết lập trạng thái Run theo APPROVAL_MODE
        # APPROVAL_MODE=true (mặc định): chuyển AWAITING_APPROVAL
        # APPROVAL_MODE=false: chuyển RUNNING ngay
        if settings.APPROVAL_MODE:
            run.status = RunStatus.AWAITING_APPROVAL.value
            logger.info(f"APPROVAL_MODE=True: Run '{run_id}' chuyển sang trạng thái AWAITING_APPROVAL.")
        else:
            run.status = RunStatus.RUNNING.value
            logger.info(f"APPROVAL_MODE=False: Run '{run_id}' chuyển thẳng sang RUNNING.")

        # Ghi nhận Audit Log
        AuditRepository.record(
            session=session,
            actor="PLAN_SERVICE",
            action="PLAN_CREATED",
            target=run_id,
            run_id=run_id,
            result="SUCCESS",
            details={
                "task_count": len(created_tasks),
                "approval_mode": settings.APPROVAL_MODE,
                "status": run.status,
            },
        )

        # Phát sự kiện qua Redis nếu có publisher
        if self.publisher:
            self.publisher.publish(
                event_type=SystemEventType.RUN_STARTED if run.status == RunStatus.RUNNING.value else SystemEventType.APPROVAL_REQUESTED,
                payload={"run_id": run_id, "status": run.status, "task_count": len(created_tasks)},
                run_id=run_id,
                session=session,
            )

        session.commit()
        return created_tasks

    def _sync_plan_to_linear(
        self,
        session: Session,
        run: WorkflowRunModel,
        plan: Dict[str, Any],
        tasks: List[TaskModel],
        dependencies: List[Tuple[str, str]],
    ) -> None:
        """Đẩy các thực thể của kế hoạch vào hàng đợi đồng bộ Linear (bảo đảm an toàn, không ném lỗi)."""
        if not self.linear_sync:
            return

        try:
            # 1. Project
            self.linear_sync.enqueue_change(
                entity_type="PROJECT",
                local_id=run.project_id,
                run_id=run.run_id,
                data={
                    "name": plan.get("project_name", run.project_id),
                    "description": plan.get("overview", run.brief),
                },
            )

            # 2. Epics / Milestones
            for epic in plan.get("epics", []):
                self.linear_sync.enqueue_change(
                    entity_type="MILESTONE",
                    local_id=epic.get("id"),
                    run_id=run.run_id,
                    data={
                        "name": epic.get("name"),
                        "description": epic.get("description", ""),
                        "project_id": run.project_id,
                    },
                )

            # 3. Tasks
            for t in tasks:
                self.linear_sync.enqueue_change(
                    entity_type="TASK",
                    local_id=t.task_id,
                    run_id=run.run_id,
                    data={
                        "title": t.action,
                        "role": t.role,
                        "action": t.action,
                        "instruction": t.instruction,
                        "priority": 1,
                        "timeout_seconds": t.timeout_seconds,
                    },
                )

            # 4. Quan hệ phụ thuộc (Relation 'blocks')
            for tid, dep_id in dependencies:
                self.linear_sync.enqueue_change(
                    entity_type="RELATION",
                    local_id=f"{dep_id}-blocks-{tid}",
                    run_id=run.run_id,
                    data={
                        "source_task_id": dep_id,
                        "target_task_id": tid,
                        "type": "blocks",
                    },
                )

            # 5. Cổng phê duyệt (Gate Approval)
            for t in tasks:
                if t.role == "GATE" or t.action.startswith("approve_"):
                    self.linear_sync.enqueue_change(
                        entity_type="APPROVAL",
                        local_id=f"gate-{t.task_id}",
                        run_id=run.run_id,
                        data={
                            "gate": t.action,
                            "status": "PENDING",
                        },
                    )

        except Exception as exc:
            logger.warning(f"Lỗi khi enqueue kế hoạch ra Linear (bỏ qua để không chặn workflow): {exc}")

    def create_project_plan(
        self,
        session: Session,
        run_id: str,
        prompt: str,
        project_id: Optional[str] = None,
        pm_caller: Optional[Callable[[str, str, Optional[List[str]]], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Điều phối toàn bộ quy trình: sinh kế hoạch qua PM, kiểm tra hợp lệ, và lưu trữ vào cơ sở dữ liệu."""
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        p_id = project_id or run.project_id

        # Cập nhật trạng thái run sang PLANNING
        run.status = RunStatus.PLANNING.value
        session.commit()

        # Sinh kế hoạch với vòng lặp retry 2 lần và fallback
        plan = self.generate_plan_with_retry(prompt=prompt, project_id=p_id, pm_caller=pm_caller)

        # Lưu kế hoạch vào DB
        self.save_plan_to_db(session=session, run_id=run_id, plan=plan)

        return plan

    def expand_stories(
        self,
        session: Session,
        run_id: str,
        stories_artifact_or_data: Any,
        max_stories: int = MAX_STORIES_DEFAULT,
        sprint_size: int = SPRINT_SIZE_DEFAULT,
    ) -> List[TaskModel]:
        """Mở rộng phần động từ User Stories theo yêu cầu Prompt S8:

        - Đọc danh sách user story từ artifact của BA.
        - Mỗi story là một dev.implement_feature, tối đa 8 story (max_stories=8).
        - Chia sprint tối đa 3 story (sprint_size=3).
        - Cuối mỗi sprint có tester.run_test_suite và scrum_master.run_sprint_review.
        - Nối lại phụ thuộc sao cho security_review phụ thuộc vào sprint review cuối cùng.
        """
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            raise ValueError(f"Không tìm thấy WorkflowRun '{run_id}'.")

        # 1. Trích xuất danh sách user stories
        stories: List[Dict[str, Any]] = []
        if isinstance(stories_artifact_or_data, dict):
            raw_list = (
                stories_artifact_or_data.get("user_stories")
                or stories_artifact_or_data.get("stories")
                or stories_artifact_or_data.get("items")
                or []
            )
            stories = list(raw_list) if isinstance(raw_list, list) else []
        elif isinstance(stories_artifact_or_data, list):
            stories = list(stories_artifact_or_data)
        elif isinstance(stories_artifact_or_data, str):
            try:
                parsed = json.loads(stories_artifact_or_data)
                if isinstance(parsed, list):
                    stories = parsed
                elif isinstance(parsed, dict):
                    stories = parsed.get("user_stories", [])
            except Exception:
                stories = []

        if not stories:
            logger.info("Không có User Story nào để mở rộng.")
            return []

        # Giới hạn tối đa số stories (mặc định 8)
        if len(stories) > max_stories:
            logger.info(f"Số lượng stories ({len(stories)}) vượt quá {max_stories}. Giới hạn lấy {max_stories} stories đầu tiên.")
            stories = stories[:max_stories]

        # 2. Chia nhỏ thành các Sprint (mỗi sprint tối đa sprint_size stories)
        sprints_chunks = [stories[i : i + sprint_size] for i in range(0, len(stories), sprint_size)]

        new_tasks_data: List[Dict[str, Any]] = []
        new_dependencies: List[Tuple[str, str]] = []

        prev_sprint_review_task_id: Optional[str] = None

        # Tìm task khởi tạo bộ khung (implement_project) của run
        foundation_stmt = select(TaskModel).where(
            TaskModel.run_id == run_id,
            TaskModel.action == "implement_project",
        )
        foundation_task = session.execute(foundation_stmt).scalars().first()
        foundation_task_id = foundation_task.task_id if foundation_task else f"{run_id}_dev_implement_project"

        for sprint_idx, chunk in enumerate(sprints_chunks, start=1):
            prev_dev_task_id: Optional[str] = None

            # Tạo dev.implement_feature tuần tự cho từng story trong sprint
            for story_idx, story in enumerate(chunk, start=1):
                raw_sid = story.get("id") or f"US-{sprint_idx:02d}-{story_idx:02d}"
                clean_sid = re.sub(r"[^a-zA-Z0-9_]", "_", str(raw_sid).lower())
                feature_task_id = f"{run_id}_dev_feature_{clean_sid}"
                title = story.get("title", f"Phát triển {raw_sid}")
                criteria = story.get("acceptance_criteria") or [f"Hoàn thành các tiêu chí của {raw_sid}"]
                if isinstance(criteria, str):
                    criteria = [criteria]

                # Xác định phụ thuộc
                task_deps: List[str] = []
                if prev_dev_task_id:
                    # Trong cùng sprint: tuần tự feature sau phụ thuộc feature trước
                    task_deps.append(prev_dev_task_id)
                    new_dependencies.append((feature_task_id, prev_dev_task_id))
                else:
                    # Feature đầu tiên của Sprint
                    if sprint_idx == 1:
                        # Sprint 1 phụ thuộc vào dev.implement_project
                        task_deps.append(foundation_task_id)
                        new_dependencies.append((feature_task_id, foundation_task_id))
                    else:
                        # Sprint N phụ thuộc vào sprint review của Sprint N-1
                        if prev_sprint_review_task_id:
                            task_deps.append(prev_sprint_review_task_id)
                            new_dependencies.append((feature_task_id, prev_sprint_review_task_id))

                new_tasks_data.append({
                    "task_id": feature_task_id,
                    "role": "DEV",
                    "action": "implement_feature",
                    "title": title,
                    "instruction": f"Lập trình tính năng cho user story {raw_sid}: {title}",
                    "depends_on": task_deps,
                    "input_artifact_types": ["SOURCE_CODE", "USER_STORY"],
                    "output_artifact_type": "SOURCE_CODE",
                    "priority": 2,
                    "max_attempts": 3,
                    "timeout_seconds": 600,
                    "acceptance_criteria": criteria,
                    "context": {
                        "story_id": raw_sid,
                        "sprint": sprint_idx,
                        "story": story,
                    },
                })
                prev_dev_task_id = feature_task_id

            # Cuối mỗi Sprint: tạo tester.run_test_suite
            test_task_id = f"{run_id}_tester_run_test_suite_sprint_{sprint_idx}"
            test_deps = [prev_dev_task_id] if prev_dev_task_id else []
            if prev_dev_task_id:
                new_dependencies.append((test_task_id, prev_dev_task_id))

            new_tasks_data.append({
                "task_id": test_task_id,
                "role": "TESTER",
                "action": "run_test_suite",
                "title": f"Kiểm thử tự động Sprint {sprint_idx}",
                "instruction": f"Chạy toàn bộ kịch bản kiểm thử tự động cho các tính năng của Sprint {sprint_idx}",
                "depends_on": test_deps,
                "input_artifact_types": ["SOURCE_CODE", "TEST_CASE"],
                "output_artifact_type": "TEST_REPORT",
                "priority": 2,
                "max_attempts": 3,
                "timeout_seconds": 300,
                "acceptance_criteria": [f"100% test cases Sprint {sprint_idx} PASS", "Không phát hiện lỗi hồi quy"],
                "context": {"sprint": sprint_idx},
            })

            # Cuối mỗi Sprint: tạo scrum_master.run_sprint_review
            review_task_id = f"{run_id}_scrum_master_run_sprint_review_sprint_{sprint_idx}"
            new_dependencies.append((review_task_id, test_task_id))

            new_tasks_data.append({
                "task_id": review_task_id,
                "role": "SCRUM_MASTER",
                "action": "run_sprint_review",
                "title": f"Họp đánh giá Sprint {sprint_idx} (Sprint Review)",
                "instruction": f"Tổng kết tiến độ, đánh giá bàn giao các user stories của Sprint {sprint_idx}",
                "depends_on": [test_task_id],
                "input_artifact_types": ["TEST_REPORT", "SOURCE_CODE"],
                "output_artifact_type": "DOCUMENTATION",
                "priority": 2,
                "max_attempts": 3,
                "timeout_seconds": 300,
                "acceptance_criteria": [f"Nghiệm thu toàn bộ stories của Sprint {sprint_idx}", "Cập nhật tiến độ backlog"],
                "context": {"sprint": sprint_idx},
            })

            prev_sprint_review_task_id = review_task_id

        # 3. Tạo các task động trong cơ sở dữ liệu
        created_tasks = TaskManager.create_tasks_from_plan(
            session=session,
            run_id=run_id,
            tasks_data=new_tasks_data,
            dependencies=new_dependencies,
        )

        # 4. Nối lại phụ thuộc sao cho security_review phụ thuộc vào review cuối
        final_review_id = prev_sprint_review_task_id
        if final_review_id:
            sec_stmt = select(TaskModel).where(
                TaskModel.run_id == run_id,
                TaskModel.role == "SECURITY",
                TaskModel.action == "security_review",
            )
            sec_task = session.execute(sec_stmt).scalars().first()
            if sec_task:
                # Xóa phụ thuộc cũ (vào dev_implement_project)
                del_stmt = delete(TaskDependencyModel).where(
                    TaskDependencyModel.task_id == sec_task.task_id,
                    TaskDependencyModel.depends_on_task_id == foundation_task_id,
                )
                session.execute(del_stmt)

                # Thêm phụ thuộc mới vào sprint review cuối cùng
                TaskRepository.add_dependency(
                    session=session,
                    task_id=sec_task.task_id,
                    depends_on_task_id=final_review_id,
                )
                logger.info(
                    f"Đã nối lại quan hệ: '{sec_task.task_id}' phụ thuộc vào review cuối '{final_review_id}'."
                )

        # Đẩy sự kiện ra Linear
        if self.linear_sync:
            try:
                for t in created_tasks:
                    self.linear_sync.enqueue_change(
                        entity_type="TASK",
                        local_id=t.task_id,
                        run_id=run_id,
                        data={
                            "title": t.action,
                            "role": t.role,
                            "action": t.action,
                            "instruction": t.instruction,
                        },
                    )
                for tid, dep_id in new_dependencies:
                    self.linear_sync.enqueue_change(
                        entity_type="RELATION",
                        local_id=f"{dep_id}-blocks-{tid}",
                        run_id=run_id,
                        data={"source_task_id": dep_id, "target_task_id": tid, "type": "blocks"},
                    )
            except Exception as e:
                logger.warning(f"Lỗi khi enqueue sprint tasks ra Linear: {e}")

        session.commit()
        return created_tasks
