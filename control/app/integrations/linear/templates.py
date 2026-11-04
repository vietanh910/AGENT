"""templates.py - Mẫu nội dung Issue và Comment đồng bộ lên Linear.

Tuân thủ quy tắc Prompt S7 mục 5:
- Mẫu Issue:
  + Agent chịu trách nhiệm, máy chạy (ví dụ PC1..PC5)
  + Mô tả yêu cầu, Acceptance Criteria
  + Phụ thuộc, task_id và run_id
  + Link PR hoặc artifact
- Mẫu Comment kết quả:
  + Dạng "[Developer Agent • dev-01] tóm tắt, lần thử, thời lượng, link"
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


ROLE_DISPLAY_NAMES = {
    "PM": "Product Manager Agent",
    "DEVOPS": "DevOps Agent",
    "SA": "System Architect Agent",
    "DBA": "Database Administrator Agent",
    "BA": "Business Analyst Agent",
    "PO": "Product Owner Agent",
    "DEV": "Developer Agent",
    "TESTER": "Quality Assurance Tester Agent",
    "SCRUM_MASTER": "Scrum Master Agent",
    "SECURITY": "Security Specialist Agent",
}

AGENT_MACHINES_DEFAULT = {
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
}


def render_issue_title(task: Dict[str, Any]) -> str:
    """Tạo tiêu đề issue trên Linear: [ROLE] Action name: Task ID."""
    role = task.get("role", "AGENT")
    action = task.get("action", "task").replace("_", " ").title()
    task_id = task.get("task_id", "")
    return f"[{role}] {action}: {task_id}"


def render_issue_description(
    task: Dict[str, Any],
    machine: Optional[str] = None,
    dependencies: Optional[List[str]] = None,
    pr_link: Optional[str] = None,
    artifact_links: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Tạo mô tả chi tiết cho issue theo cấu trúc chuẩn."""
    role = task.get("role", "AGENT")
    agent_id = task.get("agent_id", "unassigned")
    role_display = ROLE_DISPLAY_NAMES.get(role, f"{role} Agent")
    assigned_machine = machine or AGENT_MACHINES_DEFAULT.get(agent_id, "PCx")
    action = task.get("action", "")
    instruction = task.get("instruction", "Không có hướng dẫn cụ thể.")
    task_id = task.get("task_id", "")
    run_id = task.get("run_id", "")

    deps_str = ", ".join(f"`{d}`" for d in dependencies) if dependencies else "*Không có*"
    pr_str = f"[{pr_link}]({pr_link})" if pr_link else "*Chưa có (đang cập nhật)*"

    arts_lines = []
    if artifact_links:
        for art in artifact_links:
            arts_lines.append(f"- **{art.get('name', 'Artifact')}** (`{art.get('type', '')}`): `{art.get('uri', '')}`")
    arts_str = "\n".join(arts_lines) if arts_lines else "*Chưa có artifact kết quả*"

    return f"""### 🤖 Agent Phụ Trách
- **Tên Agent**: `{role_display} • {agent_id}`
- **Máy Chạy (Host)**: `{assigned_machine}`
- **Vai Trò**: `{role}`

### 📋 Mô Tả Yêu Cầu
{instruction}

### ✅ Acceptance Criteria
1. Thực thi hoàn tất tác vụ action `{action}` theo đúng chuẩn Agent Contract.
2. Không sinh ngoại lệ hoặc lỗi không kiểm soát trong quá trình xử lý.
3. Xuất đầy đủ artifact kết quả kèm mã băm SHA-256 đối soát.

### 🔗 Phụ Thuộc & Ngữ Cảnh Hệ Thống
- **Task ID**: `{task_id}`
- **Run ID**: `{run_id}`
- **Phụ thuộc các task**: {deps_str}
- **Pull Request**: {pr_str}

### 📦 Artifacts Đầu Ra
{arts_str}

---
*Ghi chú: Issue này được sinh và đồng bộ tự động từ PC1 Control Node. Vui lòng không thay đổi người gán (assignee).*
"""


def render_gate_issue_description(
    run_id: str,
    gate: str,
    project_id: str,
    brief: str,
) -> str:
    """Tạo mô tả cho Gate Approval Issue trên Linear."""
    return f"""### 🛡️ Cổng Phê Duyệt An Toàn (Approval Gate)
- **Cổng Kiểm Soát**: `{gate}`
- **Run ID**: `{run_id}`
- **Mã Dự Án**: `{project_id}`

### 📝 Tóm Tắt Dự Án
{brief}

### ⚙️ Hướng Dẫn Phê Duyệt
Hệ thống hỗ trợ 2 cách phê duyệt:
1. **Chuyển trạng thái**: Kéo issue này sang trạng thái **Done** để duyệt, hoặc **Canceled** để từ chối.
2. **Bình luận lệnh**:
   - Gõ comment `/approve` để phê duyệt bước tiếp theo.
   - Gõ comment `/reject <lý do>` để từ chối pipeline.

---
*PC1 Control Node đang chờ quyết định của bạn qua cơ chế Polling.*
"""


def render_result_comment(
    task: Dict[str, Any],
    result_status: str,
    attempt: int = 1,
    duration_sec: Optional[float] = None,
    link: Optional[str] = None,
    summary: Optional[str] = None,
) -> str:
    """Tạo comment báo cáo kết quả hoàn thành task:

    Mẫu: "[Developer Agent • dev-01] tóm tắt, lần thử, thời lượng, link"
    """
    role = task.get("role", "DEV")
    agent_id = task.get("agent_id", "dev-01")
    role_display = ROLE_DISPLAY_NAMES.get(role, f"{role} Agent")

    duration_str = f"{duration_sec:.1f}s" if duration_sec is not None else "N/A"
    link_str = link or "N/A"

    if summary:
        action_summary = summary
    elif result_status == "COMPLETED":
        action_summary = f"Hoàn thành thành công tác vụ {task.get('action', '')}"
    elif result_status == "BLOCKED":
        action_summary = f"Tác vụ bị CHẶN (cần bổ sung thông tin context)"
    else:
        action_summary = f"Tác vụ thất bại với trạng thái {result_status}"

    return f"[{role_display} • {agent_id}] {action_summary}, lần thử: {attempt}, thời lượng: {duration_str}, link: {link_str}"
