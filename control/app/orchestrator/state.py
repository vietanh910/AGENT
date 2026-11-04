"""state.py - Cấu trúc trạng thái luồng thực thi (WorkflowState) cho LangGraph Orchestrator.

Tuân thủ nguyên tắc Prompt S9:
- State tối thiểu: run_id, bộ đếm vòng sửa lỗi (fix_loop_count), cờ dừng (stop_requested).
- Trạng thái thực tế và bền vững luôn nằm trong các bảng MySQL (Single Source of Truth).
- Graph chỉ là bộ điều khiển luồng (Control Flow Engine).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from typing_extensions import TypedDict


class WorkflowState(TypedDict, total=False):
    """Trạng thái luồng thực thi của Orchestrator truyền qua lại giữa các node trong StateGraph."""

    # Nhận diện
    run_id: str
    project_id: str
    brief: str

    # Các biến đếm và cờ kiểm soát bảo vệ (Guards)
    fix_loop_count: int  # Bộ đếm vòng sửa lỗi tự động (tối đa MAX_FIX_LOOPS=3)
    stop_requested: bool  # Cờ dừng run khẩn cấp (cancel / abort)
    is_paused: bool  # Cờ tạm dừng run (pause)
    current_stage: str  # Giai đoạn hiện tại (scoping, design, feature, etc.)

    # Trạng thái điều phối & sự kiện vừa nhận
    last_event: Optional[Dict[str, Any]]  # Sự kiện nhận từ Redis Streams gần nhất
    last_processed_task_id: Optional[str]  # Task vừa hoàn thành / xử lý xong
    processed_task_ids: List[str]  # Danh sách task_id đã được process_result xử lý
    next_node: Optional[str]  # Tên node tiếp theo sau khi đánh giá router

    # Thống kê tiến độ tức thời
    active_tasks_count: int
    ready_tasks_count: int
    completed_tasks_count: int
    total_tasks_count: int

    # Báo cáo lỗi / lý do dừng
    error_message: Optional[str]
    is_completed: bool
    is_failed: bool
    is_blocked: bool
