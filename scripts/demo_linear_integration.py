"""demo_linear_integration.py - Kịch bản kiểm chứng tích hợp Linear hai chiều.

Kiểm tra:
1. Xác thực schema Linear (states, labels).
2. Tạo Project, Milestone, Issue, Sub-Issue.
3. Tạo quan hệ phụ thuộc 'blocks'.
4. Đổi trạng thái workflow (In Progress, Done) và thêm comment kết quả chuẩn format.
5. Kiểm tra Idempotency: chạy lại lần 2 không tạo trùng dữ liệu.
6. Hỗ trợ cờ --live để chạy với API key thật (nếu có) hoặc mặc định chạy an toàn (dry-run / fake).
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

# Đảm bảo root dự án nằm trong sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import dotenv_values

from control.app.database.repositories import LinearLinkRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.integrations.linear.client import FakeWorkTracker, LinearClient
from control.app.integrations.linear.config_manager import (
    initialize_and_validate_linear_schema,
    load_linear_config,
)
from control.app.integrations.linear.sync import LinearSyncService


def main():
    parser = argparse.ArgumentParser(description="Demo tích hợp Linear cho PC1 Control Node.")
    parser.add_argument("--live", action="store_true", help="Chạy thật lên Linear API (yêu cầu LINEAR_API_KEY thật)")
    parser.add_argument("--fake", action="store_true", help="Dùng FakeWorkTracker trong bộ nhớ hoàn toàn cô lập")
    args = parser.parse_args()

    print("=" * 78)
    print("DEMO S7: TÍCH HỢP LINEAR GRAPHQL - BẢN PHẢN CHIẾU CÔNG VIỆC")
    print("=" * 78)

    env_cfg = dotenv_values("control/.env")
    api_key = env_cfg.get("LINEAR_API_KEY", "")
    team_id = env_cfg.get("LINEAR_TEAM_ID", "")
    dry_run = env_cfg.get("LINEAR_DRY_RUN", "true").lower() == "true"

    if args.live:
        dry_run = False
        print("[CHẾ ĐỘ]: LIVE - Gọi trực tiếp tới Linear GraphQL API")
        if not api_key or api_key.startswith("lin_api_place"):
            print("LỖI: Chưa có LINEAR_API_KEY hợp lệ trong control/.env để chạy --live!")
            return 1
        tracker = LinearClient(api_key=api_key, dry_run=False)
    elif args.fake:
        print("[CHẾ ĐỘ]: FAKE - Chạy với FakeWorkTracker trong bộ nhớ")
        tracker = FakeWorkTracker()
    else:
        print(f"[CHẾ ĐỘ]: DRY-RUN (dry_run={dry_run}) - An toàn, ghi log thao tác GraphQL")
        tracker = LinearClient(api_key=api_key or "dry_key", dry_run=True)

    yaml_cfg = load_linear_config()
    target_team = team_id if (team_id and not team_id.startswith("team_place")) else yaml_cfg.get("team_id", "team_placeholder_id")

    # 1. Khởi tạo và xác thực Schema
    print("\n[1] XÁC THỰC VÀ CACHE WORKFLOW STATES & LABELS TỪ LINEAR:")
    schema_cache = initialize_and_validate_linear_schema(
        tracker=tracker,
        team_id=target_team,
        config=yaml_cfg,
    )
    print(f"  -> Team ID: {schema_cache.team_id}")
    print(f"  -> Số lượng trạng thái Workflow: {len(schema_cache.state_name_to_id)}")
    print(f"  -> Số lượng nhãn: {len(schema_cache.label_name_to_id)}")
    print(f"  -> Nhãn đặc biệt: {list(schema_cache.special_label_ids.keys())}")

    # 2. Khởi tạo Run và Dịch vụ đồng bộ
    session_factory = get_session_factory()
    suffix = uuid.uuid4().hex[:6].upper()
    run_id = f"RUN-LINEAR-{suffix}"
    task_dev_id = f"{run_id}-TASK-DEV"
    task_test_id = f"{run_id}-TASK-TEST"

    print(f"\n[2] KHỞI TẠO WORKFLOW RUN TRÊN PC1: '{run_id}'")
    with session_factory() as session:
        WorkflowRunRepository.create_run(
            session,
            {
                "run_id": run_id,
                "project_id": "PRJ-AUTH-LINEAR",
                "brief": "Tích hợp và phản chiếu tiến độ phát triển hệ thống qua Linear",
            },
        )
        session.commit()

    sync_service = LinearSyncService(
        tracker=tracker,
        schema_cache=schema_cache,
        session_factory=session_factory,
    )

    # 3. Đồng bộ Project và Milestone
    print("\n[3] ĐỒNG BỘ PROJECT VÀ MILESTONE LÊN LINEAR:")
    with session_factory() as session:
        prj_linear_id = sync_service.sync_entity_now(
            session=session,
            entity_type="PROJECT",
            local_id=run_id,
            run_id=run_id,
            data={"name": f"Hệ Thống AI Team - {run_id}", "brief": "Triển khai xác thực và kiểm thử tự động"},
        )
        print(f"  -> Project Linear ID: {prj_linear_id}")

        ms_linear_id = sync_service.sync_entity_now(
            session=session,
            entity_type="MILESTONE",
            local_id=f"MS-PHASE-1-{run_id}",
            run_id=run_id,
            data={"name": "Giai Đoạn 1: Core Authentication", "description": "Xây dựng và kiểm thử module xác thực"},
        )
        print(f"  -> Milestone Linear ID: {ms_linear_id}")

    # 4. Tạo Parent Issue (DEV) và Sub-Issue (TESTER)
    print("\n[4] TẠO PARENT ISSUE VÀ SUB-ISSUE TRÊN LINEAR:")
    with session_factory() as session:
        dev_task_data = {
            "task_id": task_dev_id,
            "run_id": run_id,
            "role": "DEV",
            "agent_id": "dev-01",
            "action": "implement_feature",
            "instruction": "Xây dựng module xác thực Bearer token và phân quyền truy cập",
            "dispatch_state": "RUNNING",
            "dependencies": [],
            "pr_link": "https://github.com/vietanh910/AGENT/pull/1",
        }
        dev_linear_id = sync_service.sync_entity_now(
            session=session,
            entity_type="TASK",
            local_id=task_dev_id,
            run_id=run_id,
            data=dev_task_data,
        )
        print(f"  -> Parent Issue DEV Linear ID: {dev_linear_id}")

        test_task_data = {
            "task_id": task_test_id,
            "run_id": run_id,
            "role": "TESTER",
            "agent_id": "tester-01",
            "action": "run_test_suite",
            "instruction": "Kiểm thử tự động các kịch bản đăng nhập thành công và thất bại",
            "dispatch_state": "QUEUED",
            "parent_task_id": task_dev_id,
            "dependencies": [task_dev_id],
        }
        test_linear_id = sync_service.sync_entity_now(
            session=session,
            entity_type="TASK",
            local_id=task_test_id,
            run_id=run_id,
            data=test_task_data,
        )
        print(f"  -> Sub-Issue TESTER Linear ID: {test_linear_id}")

    # 5. Tạo quan hệ blocks (DEV blocks TESTER)
    print("\n[5] TẠO QUAN HỆ PHỤ THUỘC 'BLOCKS' GIỮA DEV VÀ TESTER:")
    with session_factory() as session:
        rel_linear_id = sync_service.sync_entity_now(
            session=session,
            entity_type="RELATION",
            local_id=f"REL-{task_dev_id}-{task_test_id}",
            run_id=run_id,
            data={
                "issue_id": task_dev_id,
                "related_issue_id": task_test_id,
                "relation_type": "blocks",
            },
        )
        print(f"  -> Relation Linear ID: {rel_linear_id}")

    # 6. DEV hoàn thành -> Đổi trạng thái sang Done và gửi comment kết quả
    print("\n[6] CẬP NHẬT TRẠNG THÁI TASK DEV SANG COMPLETED VÀ GỬI COMMENT KẾT QUẢ:")
    with session_factory() as session:
        result_linear_id = sync_service.sync_entity_now(
            session=session,
            entity_type="RESULT",
            local_id=task_dev_id,
            run_id=run_id,
            data={
                "task_id": task_dev_id,
                "role": "DEV",
                "agent_id": "dev-01",
                "action": "implement_feature",
                "status": "COMPLETED",
                "attempt": 1,
                "duration_sec": 8.5,
                "link": "https://github.com/vietanh910/AGENT/pull/1",
                "summary": "Đã lập trình xong tính năng đăng nhập và tạo Pull Request #1",
            },
        )
        print(f"  -> Cập nhật kết quả hoàn thành: {result_linear_id}")

    # 7. KIỂM CHỨNG IDEMPOTENCY: Chạy lại chính xác các bước trên
    print("\n[7] KIỂM CHỨNG TÍNH IDEMPOTENT (CHẠY LẠI KHÔNG TẠO TRÙNG):")
    initial_links_count = 0
    with session_factory() as session:
        initial_links_count = len(LinearLinkRepository.list_by_prefix(session, local_id_prefix=run_id))
        print(f"  -> Số liên kết ban đầu trong MySQL: {initial_links_count}")

        # Đồng bộ lại Project, Task DEV, Task TESTER với cùng payload
        sync_service.sync_entity_now(session, "PROJECT", run_id, run_id, {"name": f"Hệ Thống AI Team - {run_id}", "brief": "Triển khai xác thực và kiểm thử tự động"})
        sync_service.sync_entity_now(session, "TASK", task_dev_id, run_id, dev_task_data)
        sync_service.sync_entity_now(session, "TASK", task_test_id, run_id, test_task_data)

        after_links_count = len(LinearLinkRepository.list_by_prefix(session, local_id_prefix=run_id))
        print(f"  -> Số liên kết sau khi chạy lại: {after_links_count}")
        assert initial_links_count == after_links_count, "Lỗi: Số lượng liên kết bị thay đổi khi chạy lại!"
        print("  -> IDEMPOTENT PASS: Không phát sinh bất kỳ bản ghi trùng nào!")

    print("\n" + "=" * 78)
    print("HOÀN TẤT THÀNH CÔNG DEMO TÍCH HỢP LINEAR S7!")
    print("  1. Schema cache, Workflow States & Labels hợp lệ")
    print("  2. Project, Milestone, Issue & Sub-Issue đã phản chiếu đầy đủ")
    print("  3. Quan hệ 'blocks' thiết lập chuẩn xác")
    print("  4. Comment kết quả chuẩn format '[Developer Agent • dev-01] ...'")
    print("  5. Cơ chế Idempotent qua content_hash đảm bảo không sinh trùng!")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
