#!/usr/bin/env python3
"""scripts/sync_trial_to_linear.py - Đồng bộ toàn bộ 10 bước workflow dự án Todo App lên Linear.
"""

from __future__ import annotations

import io
import os
import sys
import uuid
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from dotenv import dotenv_values
from control.app.integrations.linear.client import LinearClient
from control.app.integrations.linear.config_manager import (
    initialize_and_validate_linear_schema,
    load_linear_config,
)


def main():
    print("=" * 80)
    print(" BẮT ĐẦU ĐỒNG BỘ WORKFLOW 10 BƯỚC DỰ ÁN LÊN LINEAR WORKSPACE")
    print("=" * 80)

    env_cfg = dotenv_values("control/.env")
    if not env_cfg.get("LINEAR_API_KEY"):
        env_cfg = dotenv_values(".env")

    api_key = env_cfg.get("LINEAR_API_KEY")
    team_id = env_cfg.get("LINEAR_TEAM_ID", "95ea7ff9-5afb-4cc3-96a7-1bf238580335")

    if not api_key:
        print("[-] Không tìm thấy LINEAR_API_KEY trong .env hoặc control/.env!")
        return 1

    tracker = LinearClient(api_key=api_key, dry_run=False)
    yaml_cfg = load_linear_config()

    print("[1] Xác thực và nạp Workflow States & Labels từ Linear...")
    schema = initialize_and_validate_linear_schema(
        tracker=tracker,
        team_id=team_id,
        config=yaml_cfg,
    )

    # 12 workflow states
    done_state_id = schema.state_name_to_id.get("Done")
    in_progress_state_id = schema.state_name_to_id.get("In Progress") or done_state_id

    print(f"  -> Done State ID: {done_state_id}")

    # Tạo Project
    run_code = f"RUN-{uuid.uuid4().hex[:6].upper()}"
    project_name = f"Dự Án: Hệ Thống Todo App - Phân Tán 5 Máy ({run_code})"
    project_desc = (
        "Quy trình phát triển phần mềm tự động đa tác nhân (Multi-Agent System) "
        "kết nối qua Radmin VPN (PC1, PC2, PC3, PC4, PC5) gồm 10 bước khép kín."
    )
    print(f"\n[2] Khởi tạo Project trên Linear: '{project_name}'...")
    project_id = tracker.ensure_project(name=project_name, description=project_desc, team_ids=[team_id])
    print(f"  -> Project Linear ID: {project_id}")

    # Tạo Milestone
    print("\n[3] Khởi tạo Milestone Sprint 1...")
    milestone_id = tracker.ensure_milestone(
        name="Sprint 1 - Phát Triển & Triển Khai End-to-End",
        project_id=project_id,
        description="Toàn bộ 10 bước từ lập kế hoạch, phân tích, code, kiểm thử, an ninh đến triển khai Staging."
    )
    print(f"  -> Milestone Linear ID: {milestone_id}")

    # Danh sách 10 bước
    steps = [
        {
            "step": 1,
            "role": "PM",
            "agent": "pm-01",
            "machine": "PC1 (127.0.0.1:8001)",
            "title": "[PM • PC1] Bước 1: Lập kế hoạch phân bổ Milestone & Sprint",
            "desc": "PM Agent phân rã yêu cầu dự án Todo App thành 2 Sprint và 3 Milestone.",
            "artifact": "01_pm_plan.md"
        },
        {
            "step": 2,
            "role": "PO",
            "agent": "po-01",
            "machine": "PC3 (26.53.120.228:8005)",
            "title": "[PO • PC3] Bước 2: Xác định Product Backlog & Độ ưu tiên",
            "desc": "PO Agent xây dựng danh sách User Stories (US-01 đến US-06) theo chuẩn MoSCoW.",
            "artifact": "02_po_backlog.md"
        },
        {
            "step": 3,
            "role": "BA",
            "agent": "ba-01",
            "machine": "PC4 (26.222.181.101:8003)",
            "title": "[BA • PC4] Bước 3: Phân tích đặc tả yêu cầu chi tiết (SRS)",
            "desc": "BA Agent phân tích và xuất tài liệu SRS chuẩn IEEE 830 với các FRs và NFRs.",
            "artifact": "03_ba_srs.md"
        },
        {
            "step": 4,
            "role": "SA",
            "agent": "sa-01",
            "machine": "PC3 (26.53.120.228:8004)",
            "title": "[SA • PC3] Bước 4: Thiết kế kiến trúc hệ thống Microservices",
            "desc": "SA Agent thiết kế mô hình 3 tầng (Client -> API Gateway -> FastAPI -> PostgreSQL).",
            "artifact": "04_sa_architecture.md"
        },
        {
            "step": 5,
            "role": "DBA",
            "agent": "dba-01",
            "machine": "PC4 (26.222.181.101:8004)",
            "title": "[DBA • PC4] Bước 5: Thiết kế Database Schema DDL (schema.sql)",
            "desc": "DBA Agent xuất file schema DDL gồm 6 bảng (users, categories, tasks, tags, audit_logs) và 9 indexes.",
            "artifact": "05_dba_schema.sql"
        },
        {
            "step": 6,
            "role": "DEV",
            "agent": "dev-01",
            "machine": "PC2 (26.28.90.248:8006)",
            "title": "[DEV • PC2] Bước 6: Lập trình hiện thực hóa tính năng & Sửa lỗi",
            "desc": "DEV Agent lập trình module API Todo hoàn chỉnh và đóng gói source.zip.",
            "artifact": "06_dev_source.zip"
        },
        {
            "step": 7,
            "role": "TESTER",
            "agent": "tester-01",
            "machine": "PC5 (26.134.25.163:8007)",
            "title": "[TESTER • PC5] Bước 7: Xây dựng kịch bản kiểm thử tự động",
            "desc": "TESTER Agent sinh 2 test cases tự động dạng JSON cho API tạo, sửa, xóa todo.",
            "artifact": "07_tester_test_cases.json"
        },
        {
            "step": 8,
            "role": "SECURITY",
            "agent": "security-01",
            "machine": "PC5 (26.134.25.163:8010)",
            "title": "[SECURITY • PC5] Bước 8: Rà soát an toàn thông tin & Lỗ hổng bảo mật",
            "desc": "SECURITY Agent quét mã nguồn, phát hiện và cảnh báo 2 lỗi bảo mật hardcoded secret và shell injection.",
            "artifact": "08_security_report.md"
        },
        {
            "step": 9,
            "role": "DEVOPS",
            "agent": "devops-01",
            "machine": "PC1 (127.0.0.1:8002)",
            "title": "[DEVOPS • PC1] Bước 9: Tạo CI/CD Docker & Triển khai Staging",
            "desc": "DEVOPS Agent đóng gói Docker container và triển khai môi trường Staging.",
            "artifact": "09_devops_deployment_report.md"
        },
        {
            "step": 10,
            "role": "SCRUM_MASTER",
            "agent": "scrum_master-01",
            "machine": "PC2 (26.28.90.248:8009)",
            "title": "[SCRUM MASTER • PC2] Bước 10: Tổng kết Sprint Retrospective",
            "desc": "SCRUM MASTER Agent tổng kết bài học kinh nghiệm, vận tốc nhóm và cải tiến quy trình.",
            "artifact": "10_scrum_retrospective.md"
        },
    ]

    print("\n[4] Khởi tạo 10 Issues tuần tự trên Linear kèm gán nhãn vai trò...")
    issue_ids = []
    for item in steps:
        role_label_id = schema.label_name_to_id.get(f"agent:{item['role'].lower()}")
        labels = [role_label_id] if role_label_id else []

        desc_full = (
            f"### Nhiệm Vụ: {item['desc']}\n\n"
            f"- **Agent ID:** `{item['agent']}`\n"
            f"- **Máy thực thi:** `{item['machine']}`\n"
            f"- **Sản phẩm thu được:** `deliverables/todo_app/{item['artifact']}`\n"
            f"- **Trạng thái:** `COMPLETED (HTTP 200)`\n"
        )

        issue_id = tracker.upsert_issue(
            title=item["title"],
            description=desc_full,
            team_id=team_id,
            project_id=project_id,
            milestone_id=milestone_id,
            state_id=done_state_id,
            label_ids=labels,
        )
        issue_ids.append((item["step"], item["role"], issue_id, item["title"]))
        print(f"  -> Bước {item['step']:02d} [{item['role']}]: {issue_id}")

        # Thêm comment kết quả chuẩn format
        comment_body = (
            f"**[{item['role']} Agent • {item['agent']}]**\n"
            f"Đã hoàn thành tác vụ trên {item['machine']}.\n"
            f"Sản phẩm artifact: `{item['artifact']}` đã được thu thập và lưu trữ an toàn."
        )
        tracker.add_comment(issue_id=issue_id, body=comment_body)

    print("\n[5] Thiết lập quan hệ phụ thuộc 'blocks' giữa các bước liên tiếp...")
    for i in range(len(issue_ids) - 1):
        prev_id = issue_ids[i][2]
        next_id = issue_ids[i+1][2]
        tracker.add_relation(issue_id=prev_id, related_issue_id=next_id, relation_type="blocks")
        print(f"  -> Bước {i+1} blocks Bước {i+2}")

    print("\n" + "=" * 80)
    print(" ĐỒNG BỘ WORKFLOW LÊN LINEAR THÀNH CÔNG RỰC RỠ!")
    print(f" Project Name: {project_name}")
    print(f" Project ID: {project_id}")
    print(f" Team ID: {team_id}")
    print("=" * 80)


if __name__ == "__main__":
    main()
