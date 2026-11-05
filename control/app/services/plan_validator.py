"""plan_validator.py - Trình kiểm tra kế hoạch thực thi tất định (Deterministic Plan Validator).

Nguyên tắc cốt lõi (Prompt S8):
- PM (LLM) chỉ đề xuất nội dung kế hoạch.
- Khung quy trình, giới hạn và tính hợp lệ bắt buộc phải do mã tất định kiểm tra và quyết định.
- Tuyệt đối không thực thi kế hoạch nếu không vượt qua khâu kiểm định này.

Các kiểm tra:
1. Mọi (role, action) có trong ACTION_MAP và có capability trong CAPABILITY_REGISTRY.
   Hỗ trợ vai trò kiểm soát nội bộ GATE (approve_scope, approve_prod).
2. Kiểm tra đồ thị không có chu trình (DAG) bằng thuật toán Kahn / Topo sort.
3. Không có task nào tham chiếu tới task_id không tồn tại (không có dangling references).
4. Số lượng task không vượt quá MAX_TASKS_PER_RUN (mặc định 60).
5. Mọi task đều có acceptance_criteria không rỗng và output_artifact_type hợp lệ theo ArtifactType.
6. Các bước bắt buộc từ khung software_delivery.yaml phải có mặt theo đúng thứ tự (không bỏ qua kiểm thử hoặc bảo mật).
7. Trả về danh sách lỗi cụ thể, rõ ràng, dễ đọc.
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from agent_contract.enums import AgentRole, ArtifactType
from agent_contract.registry import ACTION_MAP, CAPABILITY_REGISTRY
from control.app.config import settings

logger = logging.getLogger(__name__)

# Các action hợp lệ của cổng kiểm soát GATE
GATE_ACTIONS: Set[str] = {
    "approve_scope",
    "approve_prod",
    "approve_deploy",
    "gate_approval",
}

# Danh sách bước bắt buộc theo thứ tự (Software Delivery Pipeline)
MANDATORY_STEPS: List[Tuple[str, str]] = [
    ("PO", "define_backlog"),
    ("BA", "write_requirements"),
    ("BA", "write_user_stories"),
    ("GATE", "approve_scope"),
    ("SA", "design_architecture"),
    ("DBA", "design_schema"),
    ("TESTER", "create_test_cases"),
    ("DEV", "implement_project"),
    ("SECURITY", "security_review"),
    ("DEVOPS", "generate_ci_config"),
    ("DEVOPS", "build_image"),
    ("DEVOPS", "deploy"),
    ("DEVOPS", "health_check"),
]

CHANGE_MANDATORY_STEPS = [pair for pair in MANDATORY_STEPS
    if pair not in {("SA", "design_architecture"), ("DBA", "design_schema"), ("DEV", "implement_project")}]
CHANGE_MANDATORY_STEPS.insert(5, ("DEV", "implement_feature"))
CHANGE_MANDATORY_STEPS.insert(6, ("TESTER", "run_test_suite"))


@dataclass
class PlanValidationResult:
    """Kết quả kiểm tra kế hoạch từ PlanValidator."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    topological_order: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.valid


class PlanValidator:
    """Kiểm tra kế hoạch thực thi DAG một cách tất định và nghiêm ngặt."""

    def __init__(self, max_tasks: Optional[int] = None) -> None:
        self.max_tasks = max_tasks if max_tasks is not None else settings.MAX_TASKS_PER_RUN

    def validate(self, plan: Dict[str, Any]) -> PlanValidationResult:
        """Thực hiện toàn bộ các bước kiểm tra hợp lệ của kế hoạch.

        Args:
            plan: Dict chứa danh sách tasks và cấu trúc kế hoạch.

        Returns:
            PlanValidationResult(valid, errors, topological_order)
        """
        errors: List[str] = []

        if not isinstance(plan, dict):
            return PlanValidationResult(valid=False, errors=["Kế hoạch phải là một JSON/Dictionary object hợp lệ."])

        tasks = plan.get("tasks", [])
        if not isinstance(tasks, list):
            return PlanValidationResult(valid=False, errors=["Trường 'tasks' trong kế hoạch phải là danh sách."])

        if not tasks:
            return PlanValidationResult(valid=False, errors=["Kế hoạch không chứa bất kỳ task nào."])

        # 1. Kiểm tra giới hạn số lượng task
        if len(tasks) > self.max_tasks:
            errors.append(
                f"Số lượng task ({len(tasks)}) vượt quá giới hạn tối đa cho phép ({self.max_tasks} tasks)."
            )

        # Chuẩn bị danh sách ID và map task
        task_id_set: Set[str] = set()
        task_map: Dict[str, Dict[str, Any]] = {}
        for idx, t in enumerate(tasks):
            if not isinstance(t, dict):
                errors.append(f"Phần tử task thứ {idx} không phải là dictionary.")
                continue
            tid = str(t.get("task_id") or t.get("id") or "").strip()
            if not tid:
                errors.append(f"Task thứ {idx} không có 'task_id' hoặc 'id'.")
                continue
            if tid in task_id_set:
                errors.append(f"Trùng lặp mã task_id: '{tid}'.")
            task_id_set.add(tid)
            task_map[tid] = t

        # Nếu đã có lỗi cơ bản về cấu trúc ID thì trả về sớm
        if errors:
            return PlanValidationResult(valid=False, errors=errors)

        # 2. Kiểm tra (role, action), capability, acceptance_criteria, output_artifact_type cho từng task
        valid_artifact_types = {e.value for e in ArtifactType}
        for tid, t in task_map.items():
            role_raw = t.get("role")
            action = t.get("action")

            if not role_raw:
                errors.append(f"Task '{tid}' thiếu thông tin vai trò (role).")
                continue
            if not action:
                errors.append(f"Task '{tid}' thiếu hành động (action).")
                continue

            role_str = role_raw.value if isinstance(role_raw, AgentRole) else str(role_raw).strip()
            action_str = str(action).strip()

            # Kiểm tra vai trò GATE đặc biệt
            if role_str == "GATE":
                if action_str not in GATE_ACTIONS:
                    errors.append(
                        f"Task '{tid}' có vai trò GATE nhưng action '{action_str}' không hợp lệ. "
                        f"Phải là một trong: {sorted(list(GATE_ACTIONS))}."
                    )
            else:
                # Kiểm tra role có trong AgentRole enum
                try:
                    agent_role = AgentRole(role_str)
                except ValueError:
                    errors.append(
                        f"Task '{tid}' có vai trò '{role_str}' không hợp lệ. "
                        f"Chấp nhận: {[e.value for e in AgentRole]} hoặc 'GATE'."
                    )
                    continue

                # Kiểm tra action có trong ACTION_MAP[agent_role]
                role_actions = ACTION_MAP.get(agent_role, {})
                if action_str not in role_actions:
                    errors.append(
                        f"Task '{tid}': Action '{action_str}' không được hỗ trợ cho vai trò '{role_str}'. "
                        f"Các action hợp lệ: {list(role_actions.keys())}."
                    )
                else:
                    # Kiểm tra capability tương ứng có trong CAPABILITY_REGISTRY[agent_role]
                    required_cap = role_actions[action_str]
                    agent_caps = CAPABILITY_REGISTRY.get(agent_role, [])
                    if required_cap not in agent_caps:
                        errors.append(
                            f"Task '{tid}': Capability '{required_cap}' của action '{action_str}' "
                            f"không tồn tại trong CAPABILITY_REGISTRY cho vai trò '{role_str}'."
                        )

            # Kiểm tra acceptance_criteria
            criteria = t.get("acceptance_criteria") or t.get("acceptance_criteria_template")
            if not criteria:
                errors.append(f"Task '{tid}' thiếu tiêu chí chấp nhận (acceptance_criteria không được để trống).")
            elif isinstance(criteria, list):
                if not any(str(c).strip() for c in criteria if c):
                    errors.append(f"Task '{tid}' có acceptance_criteria rỗng.")
            elif isinstance(criteria, str):
                if not criteria.strip():
                    errors.append(f"Task '{tid}' có acceptance_criteria rỗng.")
            else:
                errors.append(f"Task '{tid}' có định dạng acceptance_criteria không hợp lệ (phải là list hoặc string).")

            # Kiểm tra output_artifact_type nếu có khai báo
            output_type = t.get("output_artifact_type")
            if output_type is not None and str(output_type).strip() != "":
                out_str = str(output_type).strip()
                if out_str not in valid_artifact_types:
                    errors.append(
                        f"Task '{tid}' có output_artifact_type '{out_str}' không hợp lệ. "
                        f"Phải thuộc ArtifactType: {sorted(list(valid_artifact_types))}."
                    )

        # 3. Xây dựng đồ thị và kiểm tra chu trình (Cycle) & Dangling References
        adj: Dict[str, List[str]] = defaultdict(list)
        in_degree: Dict[str, int] = {tid: 0 for tid in task_id_set}

        # Thu thập các quan hệ phụ thuộc từ từng task (field 'depends_on')
        # và từ top-level plan.get("dependencies", [])
        explicit_deps: List[Tuple[str, str]] = []
        for tid, t in task_map.items():
            deps = t.get("depends_on", [])
            if isinstance(deps, list):
                for dep in deps:
                    explicit_deps.append((tid, str(dep).strip()))

        top_deps = plan.get("dependencies", [])
        if isinstance(top_deps, list):
            for item in top_deps:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    explicit_deps.append((str(item[0]).strip(), str(item[1]).strip()))

        # Kiểm tra dangling references và cập nhật bậc vào (in_degree)
        seen_edges: Set[Tuple[str, str]] = set()
        for u, v in explicit_deps:
            if u not in task_id_set:
                errors.append(f"Unknown dependency consumer: '{u}'")
                continue
            if not v:
                continue
            if v not in task_id_set:
                errors.append(f"Task '{u}' phụ thuộc vào task không tồn tại: '{v}' (Dangling reference).")
                continue
            if u == v:
                errors.append(f"Task '{u}' tự phụ thuộc vào chính nó (Vòng lặp trực tiếp).")
                continue

            edge = (v, u)  # Hướng đi: v phải xong trước u (v -> u)
            if edge not in seen_edges:
                seen_edges.add(edge)
                adj[v].append(u)
                in_degree[u] += 1

        # Nếu đã có lỗi dangling hoặc cấu trúc thì không chạy topo sort nữa
        if errors:
            return PlanValidationResult(valid=False, errors=errors)

        # 4. Thuật toán Kahn tìm Thứ tự Topo và phát hiện Chu trình
        queue = deque([tid for tid in task_id_set if in_degree[tid] == 0])
        topological_order: List[str] = []

        while queue:
            node = queue.popleft()
            topological_order.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if len(topological_order) < len(task_id_set):
            unresolved = [tid for tid, deg in in_degree.items() if deg > 0]
            errors.append(
                f"Phát hiện chu trình phụ thuộc trong kế hoạch (Cyclic Dependency)! "
                f"Các task nằm trong vòng lặp chu trình: {unresolved}."
            )
            return PlanValidationResult(valid=False, errors=errors)

        if plan.get("execution_mode") == "modules":
            is_change = plan.get("run_kind") == "change"
            baseline = {a["artifact_id"]: a for a in plan.get("baseline_artifacts", [])}
            if is_change and (not plan.get("baseline_release_id") or not baseline):
                errors.append("Change plans must pin an accepted baseline release")
            parents = defaultdict(set)
            for consumer, producer in explicit_deps:
                parents[consumer].add(producer)
            for tid, task in task_map.items():
                ancestors, pending = set(), list(parents[tid])
                while pending:
                    parent = pending.pop()
                    if parent not in ancestors:
                        ancestors.add(parent)
                        pending.extend(parents[parent])
                sources = (task.get("context") or {}).get("input_artifact_sources", {})
                inherited = (task.get("context") or {}).get("baseline_artifact_ids", {})
                if inherited and not is_change:
                    errors.append(f"Task '{tid}' uses baseline artifacts outside a change request")
                for kind in task.get("input_artifact_types", []):
                    if not sources.get(kind) and not inherited.get(kind):
                        errors.append(f"Task '{tid}' has no bound input producer for {kind}")
                for kind, ids in inherited.items():
                    if not isinstance(ids, list) or any(baseline.get(i, {}).get("type") != kind for i in ids):
                        errors.append(f"Task '{tid}' has an invalid pinned baseline input for {kind}")
                for kind, producers in sources.items():
                    if not isinstance(producers, list):
                        errors.append(f"Task '{tid}' has invalid artifact sources for {kind}")
                        continue
                    for producer in producers:
                        if producer not in ancestors or task_map.get(producer, {}).get("output_artifact_type") != kind:
                            errors.append(f"Task '{tid}' binds {kind} to an invalid/non-ancestor producer '{producer}'")

        # 5. Kiểm tra các bước bắt buộc từ khung software_delivery.yaml
        # Mỗi bước bắt buộc (role, action) phải xuất hiện trong kế hoạch
        # Và vị trí xuất hiện của chúng trong topo sort phải tuân thủ đúng thứ tự pipeline
        pos_map = {tid: idx for idx, tid in enumerate(topological_order)}
        last_mandatory_pos = -1
        last_step_name = ""

        # Map các task trong topo order sang cặp (role, action)
        task_action_pairs: Dict[Tuple[str, str], List[int]] = defaultdict(list)
        for tid in topological_order:
            t = task_map[tid]
            r = t.get("role")
            r_str = r.value if isinstance(r, AgentRole) else str(r).strip()
            a_str = str(t.get("action", "")).strip()
            task_action_pairs[(r_str, a_str)].append(pos_map[tid])

        mandatory = CHANGE_MANDATORY_STEPS if plan.get("run_kind") == "change" else MANDATORY_STEPS
        if plan.get("run_kind") == "change":
            if any(t["action"] == "implement_project" for t in tasks):
                errors.append("A change request must extend baseline source without creating a new project")
            for flag, action in (("architecture_changed", "design_architecture"), ("database_changed", "design_schema")):
                if plan.get(flag) and not any(t["action"] == action for t in tasks):
                    errors.append(f"Missing requested design revision: {action}")
            regression = next((t for t in tasks if t.get("task_id") == "change_regression"), None)
            if not regression or regression.get("action") != "run_test_suite":
                errors.append("Change request is missing the full project regression check")
        for step_role, step_action in mandatory:
            step_key = (step_role, step_action)
            step_label = f"{step_role}.{step_action}"

            if step_key not in task_action_pairs or not task_action_pairs[step_key]:
                errors.append(
                    f"Kế hoạch thiếu bước quy trình bắt buộc: '{step_label}'. "
                    f"Không được phép bỏ qua các bước kiểm thử, bảo mật hoặc kiểm soát phạm vi."
                )
                continue

            current_pos = min(task_action_pairs[step_key])
            if current_pos < last_mandatory_pos:
                errors.append(
                    f"Thứ tự thực thi bước bắt buộc không hợp lệ: '{step_label}' (vị trí {current_pos}) "
                    f"xuất hiện trước bước tiên quyết '{last_step_name}' (vị trí {last_mandatory_pos})."
                )
            else:
                last_mandatory_pos = current_pos
                last_step_name = step_label

        is_valid = len(errors) == 0
        return PlanValidationResult(
            valid=is_valid,
            errors=errors,
            topological_order=topological_order if is_valid else [],
        )
