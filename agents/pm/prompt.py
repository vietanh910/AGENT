import json
from typing import Any, Callable

from agent_contract.models import ArtifactRef, TaskRequest

PM_SYSTEM_PROMPT = """Bạn là Project Manager (PM) AI Agent (ID: pm-01) trong dự án 'ai-team'.

QUY TẮC BẮT BUỘC CỦA VAI TRÒ PM:
1. Bạn CHỈ làm việc ở cấp độ quản lý dự án: phân tích yêu cầu sơ bộ, lập kế hoạch tổng thể (milestones, sprints), phân rã công việc (WBS/backlog), phân công vai trò, theo dõi tiến độ và báo cáo trạng thái.
2. Trong các action quản lý thông thường, không viết code hay thiết kế kỹ thuật thay BA, SA, DEV, DBA. Action recover_task là ngoại lệ được người dùng cho phép để PM xử lý phần thiếu/kẹt; kết quả vẫn phải qua kiểm thử và nghiệm thu như bình thường.
3. Khi phân rã công việc (decompose_tasks):
   - Mọi task BẮT BUỘC có đủ: task_id (TASK-xxx), title, description, assignee_role (AgentRole hợp lệ), acceptance_criteria (danh sách tiêu chí cụ thể), due (hạn chót).
   - Chỉ được phân công cho các AgentRole hợp lệ: PM, SA, BA, PO, DEV, TESTER, DEVOPS, DBA, SCRUM_MASTER, SECURITY.
4. XỬ LÝ THIẾU THÔNG TIN (BLOCKED):
   - Nếu yêu cầu quá mơ hồ hoặc thiếu các thông tin cốt lõi (như mục tiêu dự án, quy mô nhân sự, thời hạn tổng thể), Agent sẽ trả về trạng thái BLOCKED kèm mã MISSING_CONTEXT và tối đa 3 câu hỏi cần làm rõ trong details.questions.
5. Chỉ trả về DUY NHẤT một chuỗi JSON hợp lệ tuân thủ Schema được cung cấp.
"""


def check_missing_context(request: TaskRequest) -> list[str] | None:
    """Kiểm tra nếu yêu cầu quá thiếu thông tin để thực hiện thì trả về danh sách câu hỏi làm rõ (tối đa 3)."""
    instruction = request.instruction.strip().lower()

    # Kiểm tra chỉ dẫn quá ngắn hoặc rỗng
    if not instruction or len(instruction) < 10 or instruction in ["làm đi", "kế hoạch", "help", "plan"]:
        return [
            "Mục tiêu cụ thể và phạm vi của dự án là gì?",
            "Thời hạn hoàn thành dự kiến (deadline) và quy mô đội ngũ?",
            "Có tài liệu yêu cầu (SRS/PRD) hoặc tiêu chí bàn giao cụ thể nào chưa?",
        ]

    # Kiểm tra cờ flag missing_context trong context
    if request.context.get("simulate_missing_context") or request.context.get("missing_info"):
        return [
            "Quy mô nhân sự phân bổ cho dự án là bao nhiêu?",
            "Thời gian dự kiến cho từng Sprint là mấy tuần?",
            "Yêu cầu về môi trường triển khai chính (Cloud hay On-Premise)?",
        ]

    return None


def build_messages(
    system_prompt: str,
    request: TaskRequest,
    artifact_reader: Callable[[ArtifactRef | str], str] | None = None,
    output_schema: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Ghép system prompt, instruction, context và nội dung input_artifacts thành messages chuẩn."""
    system_parts = [system_prompt.strip()]
    system_parts.append(
        "\n--- QUY TẮC BẮT BUỘC ---\n"
        "1. Trả về DUY NHẤT một chuỗi JSON hợp lệ theo Schema.\n"
        "2. Không thêm bất kỳ lời dẫn hay văn bản nào bên ngoài JSON."
    )

    if output_schema:
        schema_json = json.dumps(output_schema, indent=2, ensure_ascii=False)
        system_parts.append(f"\n--- JSON SCHEMA BẮT BUỘC ---\n```json\n{schema_json}\n```")

    full_system = "\n\n".join(system_parts)

    user_parts = [
        f"Mã tác vụ (Task ID): {request.task_id}",
        f"Hành động (Action): {request.action}",
        f"Chỉ dẫn (Instruction):\n{request.instruction}",
    ]

    if request.context:
        user_parts.append(f"Ngữ cảnh (Context):\n```json\n{json.dumps(request.context, indent=2, ensure_ascii=False)}\n```")

    if request.input_artifacts:
        art_sections = ["Tài liệu đầu vào (Input Artifacts):"]
        for art in request.input_artifacts:
            header = f"- Artifact ID: {art.artifact_id} | Tên: {art.name} | Loại: {art.type.value}"
            content = ""
            if artifact_reader:
                try:
                    content = artifact_reader(art)
                except Exception as e:
                    content = f"[Lỗi đọc artifact: {e}]"
            if content:
                art_sections.append(f"{header}\nNội dung:\n```\n{content}\n```")
            else:
                art_sections.append(f"{header} (URI: {art.uri})")
        user_parts.append("\n\n".join(art_sections))

    return [
        {"role": "system", "content": full_system},
        {"role": "user", "content": "\n\n".join(user_parts)},
    ]
