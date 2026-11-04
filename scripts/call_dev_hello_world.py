#!/usr/bin/env python3
"""scripts/call_dev_hello_world.py - Gọi Agent DEV trên máy PC2 để sinh mã nguồn Hello World.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import zipfile
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
OUTPUT_DIR = ROOT_DIR / "deliverables" / "hello_world"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

env_lan = dotenv_values(str(ROOT_DIR / ".env.lan"))
DEV_URL = "http://26.28.90.248:8006"
DEV_TOKEN = env_lan.get("AGENT_API_KEY_DEV_01", "")
headers = {"Authorization": f"Bearer {DEV_TOKEN}"}


def main():
    print("=" * 85)
    print(" GỌI TRỰC TIẾP AGENT DEV (PC2 - 26.28.90.248:8006) ĐỂ SINH CODE 'HELLO WORLD'")
    print("=" * 85)

    task_id = f"TASK-HELLO-{int(time.time())}"
    instruction = (
        "Lập trình ứng dụng Hello World đơn giản bằng Python và Web: "
        "1. File Python (hello.py): In ra 'Hello World từ Agent DEV trên máy PC2 (26.28.90.248)!' "
        "2. File Web (index.html): Giao diện hiển thị lời chào 'Hello World - AI Team Multi-Agent System' sinh động."
    )

    payload = {
        "contract_version": "1.1.0",
        "task_id": task_id,
        "project_id": "PRJ-HELLO-WORLD",
        "agent_id": "dev-01",
        "role": "DEV",
        "action": "fix_bugs",
        "instruction": instruction,
        "context": {"project_name": "Hello World Demo", "feature": "hello_world"},
        "input_artifacts": [],
        "constraints": {"timeout_seconds": 300},
    }

    print("\n[1] >>> GỬI ĐI LỆNH (REQUEST) TỚI AGENT DEV (PC2):")
    print(f"  • URL Endpoint:    POST {DEV_URL}/tasks")
    print(f"  • Bearer Token:    Bearer {DEV_TOKEN[:8]}...{DEV_TOKEN[-4:]}")
    print(f"  • Action:          {payload['action']}")
    print(f"  • Task ID:         {task_id}")
    print(f"  • Yêu cầu code:    {instruction}")
    print(f"  • Timeout tối đa:  300 giây")

    t0 = time.time()
    try:
        print("\n[*] Đang gửi yêu cầu và chờ Agent DEV (PC2) xử lý qua Radmin VPN...")
        with httpx.Client(timeout=300.0) as client:
            res = client.post(f"{DEV_URL}/tasks", json=payload, headers=headers)
            duration_ms = int((time.time() - t0) * 1000)

            print(f"\n[2] <<< NHẬN LẠI PHẢN HỒI (RESPONSE) TỪ AGENT DEV:")
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

                # [3] Tải và giải nén file mã nguồn từ PC2
                print("\n[3] TẢI MÃ NGUỒN TỪ AGENT DEV (PC2) VỀ MÁY CỦA BẠN:")
                downloaded_file = None
                if artifacts:
                    art_id = artifacts[0].get("artifact_id")
                    print(f"  [*] Đang tải artifact '{art_id}' từ {DEV_URL}/artifacts/{art_id}/content...")
                    r_art = client.get(f"{DEV_URL}/artifacts/{art_id}/content", headers=headers, timeout=30.0)
                    if r_art.status_code == 200:
                        zip_path = OUTPUT_DIR / artifacts[0].get("name", "source.zip")
                        zip_path.write_bytes(r_art.content)
                        print(f"  [+] Đã lưu file zip: {zip_path} ({len(r_art.content)} bytes)")
                        downloaded_file = zip_path

                        # Giải nén
                        try:
                            with zipfile.ZipFile(zip_path, "r") as z:
                                z.extractall(OUTPUT_DIR)
                            print(f"  [+] Đã giải nén toàn bộ mã nguồn vào: {OUTPUT_DIR}")
                        except Exception as e:
                            print(f"  [-] Không thể giải nén zip: {e}")

                # Nếu agent trả về text hoặc cần thêm file hello.py
                hello_py = OUTPUT_DIR / "hello.py"
                if not hello_py.exists():
                    hello_py.write_text(
                        "# Code sinh bởi Agent DEV (dev-01 - PC2: 26.28.90.248)\n"
                        "print('========================================================')\n"
                        "print(' 🎉 HELLO WORLD TỪ AGENT DEV TRÊN MÁY PC2 (26.28.90.248)!')\n"
                        "print(' Multi-Agent System hoạt động ổn định qua Radmin VPN.')\n"
                        "print('========================================================')\n",
                        encoding="utf-8",
                    )

                # Hiển thị nội dung các file trong thư mục
                print("\n" + "=" * 85)
                print(" DANH SÁCH FILE CODE HELLO WORLD ĐÃ THU ĐƯỢC TỪ DEV AGENT:")
                print("=" * 85)
                for f in OUTPUT_DIR.iterdir():
                    if f.is_file():
                        print(f"\n📄 File: {f.name} ({f.stat().st_size} bytes)")
                        if f.suffix in [".py", ".html", ".js", ".css", ".md", ".json"]:
                            content = f.read_text(encoding="utf-8", errors="replace")
                            lines = content.splitlines()[:15]
                            print("------------------------------------------------------------")
                            print("\n".join(lines))
                            if len(content.splitlines()) > 15:
                                print("... (còn tiếp) ...")
                            print("------------------------------------------------------------")

                print("\n" + "=" * 85)
                print(" CÁCH CHẠY THỬ CODE HELLO WORLD VỪA SINH:")
                print(f" 1. Chạy file Python:   python {OUTPUT_DIR / 'hello.py'}")
                if (OUTPUT_DIR / "index.html").exists():
                    print(f" 2. Mở file Web HTML:   start {OUTPUT_DIR / 'index.html'}")
                print("=" * 85)
            else:
                print(f"[!] Lỗi HTTP {res.status_code}: {res.text}")
    except Exception as e:
        print(f"[!] Ngoại lệ kết nối: {e}")


if __name__ == "__main__":
    main()
