#!/usr/bin/env python3
"""scripts/call_sm_test.py - Gửi lệnh test tới Scrum Master Agent trên máy PC2.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import dotenv_values

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT_DIR / "deliverables" / "scrum_master_test"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

env_lan = dotenv_values(str(ROOT_DIR / ".env.lan"))
SM_URL = "http://26.28.90.248:8009"
SM_TOKEN = env_lan.get("AGENT_API_KEY_SCRUM_MASTER_01", "")
headers = {"Authorization": f"Bearer {SM_TOKEN}"}


def main():
    print("=" * 85)
    print(" GỌI TRỰC TIẾP SCRUM MASTER AGENT (PC2 - 26.28.90.248:8009)")
    print("=" * 85)

    task_id = f"TASK-SM-RETRO-{int(time.time())}"
    instruction = (
        "Tổ chức phiên họp Sprint Retrospective cho Sprint 1 của dự án Multi-Agent: "
        "1. Đánh giá kết quả bàn giao: Agent DEV trên PC2 đã hoàn thành mã nguồn Hello World. "
        "2. Đánh giá điểm tốt (What went well): Kết nối mạng Radmin VPN thông suốt giữa PC1 và PC2. "
        "3. Đề xuất cải tiến (Action items): Tối ưu hóa thời gian gọi LLM cho các tác vụ phức tạp."
    )

    payload = {
        "contract_version": "1.1.0",
        "task_id": task_id,
        "project_id": "PRJ-RETRO-TEST",
        "agent_id": "scrum_master-01",
        "role": "SCRUM_MASTER",
        "action": "run_retrospective",
        "instruction": instruction,
        "context": {
            "project_name": "Multi-Agent System Trial",
            "sprint_number": 1,
            "tasks": [
                {
                    "task_id": "TASK-DEV-HELLO",
                    "role": "DEV",
                    "status": "COMPLETED",
                    "summary": "Agent DEV đã sinh mã nguồn Python và Web thành công."
                }
            ]
        },
        "input_artifacts": [],
        "constraints": {"timeout_seconds": 1000},
    }

    print("\n[1] >>> GỬI ĐI LỆNH (REQUEST) TỚI SCRUM MASTER AGENT (PC2):")
    print(f"  • URL Endpoint:    POST {SM_URL}/tasks")
    print(f"  • Bearer Token:    Bearer {SM_TOKEN[:8]}...{SM_TOKEN[-4:]}")
    print(f"  • Action:          {payload['action']}")
    print(f"  • Task ID:         {task_id}")
    print(f"  • Yêu cầu họp:     {instruction[:140]}...")
    print(f"  • Timeout tối đa:  1000 giây")

    t0 = time.time()
    try:
        print("\n[*] Đang gửi yêu cầu và chờ Scrum Master (PC2) xử lý qua Radmin VPN...")
        with httpx.Client(timeout=1000.0) as client:
            res = client.post(f"{SM_URL}/tasks", json=payload, headers=headers)
            duration_ms = int((time.time() - t0) * 1000)

            print(f"\n[2] <<< NHẬN LẠI PHẢN HỒI (RESPONSE) TỪ SCRUM MASTER AGENT:")
            print(f"  • HTTP Status:     {res.status_code}")
            print(f"  • Thời gian xử lý: {duration_ms}ms ({duration_ms/1000:.1f}s)")

            if res.status_code == 200:
                data = res.json()
                status = data.get("status", "COMPLETED")
                summary = data.get("summary", "Đã thực thi thành công.")
                artifacts = data.get("artifacts", [])

                print(f"  • Trạng thái tác vụ: {status}")
                print(f"  • Tóm tắt:          {summary}")
                print(f"  • Artifacts trả về: {[a.get('name', a.get('artifact_id')) for a in artifacts]}")

                # [3] Tải biên bản Retrospective từ PC2
                print("\n[3] TẢI BIÊN BẢN RETROSPECTIVE TỪ PC2 VỀ MÁY CỦA BẠN:")
                saved_file = None
                if artifacts:
                    art_id = artifacts[0].get("artifact_id")
                    art_name = artifacts[0].get("name", "retrospective.md")
                    print(f"  [*] Đang tải artifact '{art_id}' từ {SM_URL}/artifacts/{art_id}/content...")
                    r_art = client.get(f"{SM_URL}/artifacts/{art_id}/content", headers=headers, timeout=30.0)
                    if r_art.status_code == 200:
                        saved_file = OUTPUT_DIR / art_name
                        saved_file.write_bytes(r_art.content)
                        print(f"  [+] Đã lưu thành công: {saved_file} ({len(r_art.content)} bytes)")

                # Hiển thị nội dung biên bản
                if saved_file and saved_file.exists():
                    print("\n" + "=" * 85)
                    print(f" NỘI DUNG BIÊN BẢN HỌP RETROSPECTIVE SINH BỞI SCRUM MASTER AGENT ({saved_file.name}):")
                    print("=" * 85)
                    content = saved_file.read_text(encoding="utf-8", errors="replace")
                    print(content)
                    print("=" * 85)
                else:
                    print("[-] Không tải được file artifact trực tiếp.")
            else:
                print(f"[!] Lỗi HTTP {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[!] Ngoại lệ kết nối: {e}")


if __name__ == "__main__":
    main()
