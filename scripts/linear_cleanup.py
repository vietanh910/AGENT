"""scripts/linear_cleanup.py - Hủy hoặc lưu trữ các Issue thuộc Workflow Run cũ trên Linear.

Giúp giải phóng quota số issue trên gói Linear Free (giới hạn ~250 issue).
Sử dụng:
    python scripts/linear_cleanup.py <run_id> [--archive | --delete]
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Đảm bảo root dự án nằm trong sys.path
repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import dotenv_values
from control.app.database.repositories import LinearLinkRepository
from control.app.database.session import get_session_factory
from control.app.integrations.linear.client import LinearClient, FakeWorkTracker


def main():
    parser = argparse.ArgumentParser(description="Dọn dẹp (Archive hoặc Delete) các Issue của Workflow Run trên Linear.")
    parser.add_argument("run_id", help="Mã Workflow Run cần dọn dẹp (ví dụ: RUN-DEMO-01)")
    parser.add_argument(
        "--action",
        choices=["archive", "delete"],
        default="archive",
        help="Hành động thực hiện: 'archive' (mặc định - an toàn) hoặc 'delete' (xóa vĩnh viễn)",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Chạy ở chế độ test với FakeWorkTracker",
    )
    args = parser.parse_args()

    run_id = args.run_id
    action = args.action

    print("=" * 70)
    print(f"LINEAR CLEANUP CHO RUN: '{run_id}' (Hành động: {action.upper()})")
    print("=" * 70)

    # 1. Tìm tất cả các liên kết Linear của run_id trong MySQL
    session_factory = get_session_factory()
    with session_factory() as session:
        links = LinearLinkRepository.list_by_prefix(session, local_id_prefix=run_id)

    if not links:
        print(f"Không tìm thấy liên kết Linear nào trong cơ sở dữ liệu cho run '{run_id}'.")
        return 0

    print(f"Tìm thấy {len(links)} đối tượng liên kết Linear cần xử lý.")

    # 2. Khởi tạo Tracker
    if args.fake:
        tracker = FakeWorkTracker()
        print("Sử dụng FakeWorkTracker (chế độ kiểm thử).")
    else:
        env_cfg = dotenv_values("control/.env")
        api_key = env_cfg.get("LINEAR_API_KEY", "")
        dry_run = env_cfg.get("LINEAR_DRY_RUN", "true").lower() == "true"
        if not api_key:
            print("Lỗi: Không tìm thấy LINEAR_API_KEY trong control/.env")
            return 1
        tracker = LinearClient(api_key=api_key, dry_run=dry_run)
        print(f"Khởi tạo LinearClient (dry_run={dry_run}).")

    success_count = 0
    fail_count = 0

    for link in links:
        print(f"  -> Đang xử lý {link.entity_type} '{link.local_id}' (Linear ID: {link.linear_id})...", end="")
        try:
            if action == "delete":
                ok = tracker.delete_issue(link.linear_id)
            else:
                ok = tracker.archive_issue(link.linear_id)

            if ok:
                print(" [THÀNH CÔNG]")
                success_count += 1
            else:
                print(" [BỎ QUA / KHÔNG TỒN TẠI]")
        except Exception as exc:
            print(f" [LỖI: {exc}]")
            fail_count += 1

    print("-" * 70)
    print(f"Hoàn tất dọn dẹp Linear cho run '{run_id}':")
    print(f"  - Thành công: {success_count}")
    print(f"  - Thất bại:   {fail_count}")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
