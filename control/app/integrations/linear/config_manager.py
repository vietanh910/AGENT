"""config_manager.py - Quản lý cấu hình Linear và xác thực tính sẵn sàng của Workflow States & Labels.

Tuân thủ quy tắc Prompt S7 mục 3:
- Đọc cấu hình từ control/linear.yaml (hoặc fallback).
- Khi khởi động: lấy ID các trạng thái và nhãn từ Linear qua WorkTracker và cache lại.
- Thiếu bất kỳ trạng thái bắt buộc hoặc nhãn đặc biệt nào (blocked, needs-approval)
  thì DỪNG NGAY LẬP TỨC với thông báo lỗi liệt kê chính xác các mục bị thiếu.
- Ánh xạ dispatch_state sang Linear state_id; gán nhãn theo role (agent:dev, ...), không dùng assignee.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import yaml

from control.app.integrations.linear.client import WorkTracker

logger = logging.getLogger(__name__)


class LinearConfigurationError(Exception):
    """Ngoại lệ khi cấu hình Linear thiếu các trạng thái hoặc nhãn bắt buộc."""
    pass


DEFAULT_CONFIG_PATH = Path("control/linear.yaml")

DEFAULT_CONFIG: Dict[str, Any] = {
    "team_id": "team_placeholder_id",
    "dispatch_state_map": {
        "QUEUED": "Backlog",
        "ASSIGNED": "Todo",
        "RUNNING": "In Progress",
        "RETRY_WAIT": "In Progress",
        "BLOCKED": "In Progress",
        "COMPLETED": "Done",
        "FAILED": "Canceled",
        "CANCELLED": "Canceled",
    },
    "special_labels": {
        "blocked": "blocked",
        "needs_approval": "needs-approval",
    },
    "role_labels": {
        "PM": "agent:pm",
        "DEVOPS": "agent:devops",
        "SA": "agent:sa",
        "DBA": "agent:dba",
        "BA": "agent:ba",
        "PO": "agent:po",
        "DEV": "agent:dev",
        "TESTER": "agent:tester",
        "SCRUM_MASTER": "agent:scrum_master",
        "SECURITY": "agent:security",
    },
    "agent_machines": {
        "pm-01": "PC1",
        "devops-01": "PC1",
        "sa-01": "PC2",
        "dba-01": "PC2",
        "ba-01": "PC3",
        "po-01": "PC3",
        "dev-01": "PC4",
        "tester-01": "PC4",
        "scrum_master-01": "PC5",
        "security-01": "PC5",
    },
    "linear_max_req_per_min": 50,
    "max_linear_issues_per_run": 50,
    "linear_poll_seconds": 20,
    "max_sync_attempts": 5,
}


@dataclass
class LinearSchemaCache:
    """Bộ nhớ đệm ánh xạ ID các trạng thái và nhãn trên Linear."""
    team_id: str
    state_name_to_id: Dict[str, str] = field(default_factory=dict)
    label_name_to_id: Dict[str, str] = field(default_factory=dict)
    dispatch_to_state_id: Dict[str, str] = field(default_factory=dict)
    special_label_ids: Dict[str, str] = field(default_factory=dict)
    role_to_label_id: Dict[str, str] = field(default_factory=dict)
    agent_machines: Dict[str, str] = field(default_factory=dict)
    max_issues_per_run: int = 50
    max_req_per_min: int = 50
    poll_seconds: int = 20
    max_sync_attempts: int = 5


def load_linear_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """Tải file linear.yaml nếu tồn tại, ngược lại trả về cấu hình mặc định."""
    path = config_path or DEFAULT_CONFIG_PATH
    if not path.exists():
        example_path = path.with_suffix(".example.yaml")
        if example_path.exists():
            path = example_path

    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                # Gộp với mặc định để không sót khóa
                merged = dict(DEFAULT_CONFIG)
                merged.update(data)
                return merged
        except Exception as exc:
            logger.warning("Không thể đọc file cấu hình %s (%s). Sử dụng cấu hình mặc định.", path, exc)

    return dict(DEFAULT_CONFIG)


def initialize_and_validate_linear_schema(
    tracker: WorkTracker,
    team_id: str,
    config: Optional[Dict[str, Any]] = None,
) -> LinearSchemaCache:
    """Lấy danh sách trạng thái và nhãn từ Linear, kiểm tra tính đầy đủ và lưu vào bộ nhớ cache.

    Nếu thiếu bất kỳ trạng thái bắt buộc nào trong dispatch_state_map hoặc nhãn đặc biệt
    (blocked, needs-approval), hàm sẽ ném LinearConfigurationError liệt kê chính xác các mục thiếu.
    """
    cfg = config or load_linear_config()
    target_team = team_id or cfg.get("team_id", "team_placeholder_id")

    # 1. Truy vấn Workflow States từ Linear
    raw_states = tracker.get_workflow_states(target_team)
    # Ánh xạ dạng {tên viết thường: id} và {tên gốc: id}
    state_map: Dict[str, str] = {}
    for st in raw_states:
        name = st.get("name", "")
        sid = st.get("id", "")
        if name and sid:
            state_map[name.lower()] = sid
            state_map[name] = sid

    # 2. Truy vấn Issue Labels từ Linear
    raw_labels = tracker.get_issue_labels(target_team)
    label_map: Dict[str, str] = {}
    for lbl in raw_labels:
        name = lbl.get("name", "")
        lid = lbl.get("id", "")
        if name and lid:
            label_map[name.lower()] = lid
            label_map[name] = lid

    # 3. Kiểm tra các trạng thái workflow bắt buộc
    dispatch_map = cfg.get("dispatch_state_map", DEFAULT_CONFIG["dispatch_state_map"])
    required_state_names: Set[str] = set(dispatch_map.values())
    missing_states: List[str] = []

    dispatch_to_state_id: Dict[str, str] = {}
    for disp_state, target_name in dispatch_map.items():
        sid = state_map.get(target_name) or state_map.get(target_name.lower())
        if not sid:
            if target_name not in missing_states:
                missing_states.append(target_name)
        else:
            dispatch_to_state_id[disp_state] = sid

    # 4. Kiểm tra các nhãn đặc biệt bắt buộc
    special_labels = cfg.get("special_labels", DEFAULT_CONFIG["special_labels"])
    missing_labels: List[str] = []
    special_label_ids: Dict[str, str] = {}

    for key, label_name in special_labels.items():
        lid = label_map.get(label_name) or label_map.get(label_name.lower())
        if not lid:
            missing_labels.append(label_name)
        else:
            special_label_ids[key] = lid

    # Nếu thiếu bất kỳ trạng thái hoặc nhãn đặc biệt nào, DỪNG VÀ BÁO LỖI RÕ RÀNG
    if missing_states or missing_labels:
        err_msg_parts = ["Cấu hình Linear thiếu các trạng thái hoặc nhãn bắt buộc trên Team/Workspace:"]
        if missing_states:
            err_msg_parts.append(f"  - Trạng thái Workflow thiếu ({len(missing_states)}): {', '.join(sorted(missing_states))}")
        if missing_labels:
            err_msg_parts.append(f"  - Nhãn đặc biệt thiếu ({len(missing_labels)}): {', '.join(sorted(missing_labels))}")
        err_msg_parts.append("Vui lòng tạo đầy đủ các trạng thái/nhãn trên trong cài đặt Linear Team trước khi khởi động.")
        full_msg = "\n".join(err_msg_parts)
        logger.error(full_msg)
        raise LinearConfigurationError(full_msg)

    # 5. Ánh xạ các nhãn Role của Agent (nếu thiếu thì log cảnh báo, không chặn)
    role_labels = cfg.get("role_labels", DEFAULT_CONFIG["role_labels"])
    role_to_label_id: Dict[str, str] = {}
    for role, lbl_name in role_labels.items():
        lid = label_map.get(lbl_name) or label_map.get(lbl_name.lower())
        if lid:
            role_to_label_id[role] = lid
        else:
            logger.info("Nhãn vai trò '%s' chưa được định nghĩa trên Linear. Task sẽ không gán nhãn này.", lbl_name)

    logger.info(
        "Đã xác thực và tải Linear schema thành công: %d workflow states, %d labels.",
        len(raw_states), len(raw_labels),
    )

    return LinearSchemaCache(
        team_id=target_team,
        state_name_to_id=state_map,
        label_name_to_id=label_map,
        dispatch_to_state_id=dispatch_to_state_id,
        special_label_ids=special_label_ids,
        role_to_label_id=role_to_label_id,
        agent_machines=cfg.get("agent_machines", DEFAULT_CONFIG["agent_machines"]),
        max_issues_per_run=int(cfg.get("max_linear_issues_per_run", 50)),
        max_req_per_min=int(cfg.get("linear_max_req_per_min", 50)),
        poll_seconds=int(cfg.get("linear_poll_seconds", 20)),
        max_sync_attempts=int(cfg.get("max_sync_attempts", 5)),
    )
