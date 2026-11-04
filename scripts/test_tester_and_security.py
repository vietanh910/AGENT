#!/usr/bin/env python3
"""scripts/test_tester_and_security.py - Gửi lệnh kiểm thử tới Agent TESTER và SECURITY trên PC5 (26.134.25.163).

Quy trình:
1. Ping kiểm tra sẵn sàng tester-01 (port 8007) và security-01 (port 8010) trên PC5.
2. Gửi tác vụ create_test_cases tới tester-01, hiển thị minh bạch Request/Response.
3. Gửi tác vụ security_review tới security-01, hiển thị minh bạch Request/Response.
4. Tải các Artifacts sinh ra về thư mục deliverables/test_tester_security/.
5. Tổng kết chi tiết kết quả thực thi.
"""

from __future__ import annotations

import io
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
import yaml
from dotenv import dotenv_values


def load_config():
    endpoints_file = ROOT_DIR / "scripts" / "endpoints.yaml"
    with open(endpoints_file, "r", encoding="utf-8") as f:
        endpoints = yaml.safe_load(f)

    env_vars = dotenv_values(str(ROOT_DIR / ".env.lan"))
    return endpoints, env_vars


def ping_agent(agent_id: str, url: str, token: str) -> bool:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=5.0) as client:
            r_health = client.get(f"{url.rstrip('/')}/health")
            r_agent = client.get(f"{url.rstrip('/')}/agent", headers=headers)
            if r_health.status_code == 200 and r_agent.status_code == 200:
                return True
    except Exception:
        pass
    return False


def download_artifact(url: str, token: str, artifact_id: str, save_path: Path) -> bool:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(f"{url.rstrip('/')}/artifacts/{artifact_id}/content", headers=headers)
            if r.status_code == 200:
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(r.content)
                return True
    except Exception as e:
        print(f"    [!] Lỗi khi tải artifact {artifact_id}: {e}")
    return False


def main():
    endpoints, env_vars = load_config()
    out_dir = ROOT_DIR / "deliverables" / "test_tester_security"
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = [
        {
            "id": "tester-01",
            "role": "TESTER",
            "machine": "PC5 (26.134.25.163)",
            "url": endpoints.get("tester-01", "http://26.134.25.163:8007"),
            "token": env_vars.get("AGENT_API_KEY_TESTER_01", ""),
            "action": "create_test_cases",
            "instruction": "Xây dựng bộ kịch bản kiểm thử (Test Cases) tự động bao phủ các chức năng API Đăng nhập, Đặt phòng và Hủy phòng với các ca kiểm thử tích cực (happy path) và biên (edge cases).",
            "artifact_prefix": "tester",
            "extra": {},
        },
        {
            "id": "security-01",
            "role": "SECURITY",
            "machine": "PC5 (26.134.25.163)",
            "url": endpoints.get("security-01", "http://26.134.25.163:8010"),
            "token": env_vars.get("AGENT_API_KEY_SECURITY_01", ""),
            "action": "security_review",
            "instruction": "Rà soát toàn diện an toàn thông tin, quét các lỗ hổng OWASP Top 10 (SQL Injection, XSS, Broken Auth, Hardcoded Secrets) và đánh giá độ an toàn kiến trúc mã nguồn.",
            "artifact_prefix": "security",
            "extra": {
                "input_artifacts": [
                    {
                        "artifact_id": "ART-SRC-001",
                        "name": "source.zip",
                        "type": "SOURCE_CODE",
                        "version": "1.0",
                        "uri": "artifacts/inbox/dev-01/source.zip",
                        "created_by": "dev-01",
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            },
        },
    ]

    print("\n" + "=" * 105)
    print(" BƯỚC 1: KIỂM TRA ĐỘ SẴN SÀNG CỦA AGENT TESTER & SECURITY TRÊN PC5 (26.134.25.163)")
    print("=" * 105)
    for t in targets:
        ok = ping_agent(t["id"], t["url"], t["token"])
        status_text = "ONLINE (SẴN SÀNG)" if ok else "OFFLINE (KHÔNG KẾT NỐI ĐƯỢC)"
        print(f" • {t['id']} ({t['role']}) tại {t['url']} -> {status_text}")
        if not ok:
            print(f"\n❌ [LỖI] Không thể kết nối tới {t['id']}. Dừng kiểm thử theo quy định an toàn!")
            sys.exit(1)

    print("\n" + "=" * 105)
    print(" BƯỚC 2: GỬI LỆNH THỰC THI KIỂM THỬ (MINH BẠCH REQUEST / RESPONSE & TIMEOUT 1000s)")
    print("=" * 105)

    summary_results = []

    for idx, t in enumerate(targets, 1):
        task_id = f"TASK-TEST-{idx:02d}-{t['id'].upper()}-{uuid.uuid4().hex[:6]}"
        payload = {
            "contract_version": "1.1.0",
            "task_id": task_id,
            "project_id": "PRJ-VERIFICATION-DEMO",
            "agent_id": t["id"],
            "role": t["role"],
            "action": t["action"],
            "instruction": t["instruction"],
            "context": {"test_run": True, "target": t["role"]},
            "input_artifacts": [],
            "constraints": {"timeout_seconds": 1000},
        }
        if t["extra"]:
            payload.update(t["extra"])

        headers = {"Authorization": f"Bearer {t['token']}"} if t["token"] else {}

        print(f"\n[{idx}/2] >>> GỬI ĐI YÊU CẦU TỚI: {t['id']} ({t['role']}) - MÁY: {t['machine']}")
        print(f"  • URL Endpoint:    POST {t['url'].rstrip('/')}/tasks")
        print(f"  • Bearer Token:    Bearer {t['token'][:8]}...{t['token'][-4:] if len(t['token']) > 12 else ''}")
        print(f"  • Tác vụ (Action): {t['action']}")
        print(f"  • Task ID:         {task_id}")
        print(f"  • Chỉ đạo:         {t['instruction']}")
        print(f"  • Timeout tối đa:  1000.0 giây")
        if t["extra"].get("input_artifacts"):
            print(f"  • Input Artifacts: {[a['name'] for a in t['extra']['input_artifacts']]}")

        t0 = time.time()
        try:
            with httpx.Client(timeout=1000.0) as client:
                res = client.post(f"{t['url'].rstrip('/')}/tasks", json=payload, headers=headers)
                duration_s = time.time() - t0
                duration_ms = int(duration_s * 1000)

                print(f"\n<<< NHẬN LẠI PHẢN HỒI TỪ {t['id']}: (Thời gian: {duration_ms}ms / {duration_s:.1f}s)")
                print(f"  • HTTP Status:     {res.status_code}")

                if res.status_code != 200:
                    print(f"  ❌ Lỗi HTTP: {res.text[:200]}")
                    print(f"  [!] Dừng hệ thống do Agent '{t['id']}' trả về lỗi.")
                    sys.exit(1)

                data = res.json()
                status = data.get("status", "COMPLETED")
                summary = data.get("summary", "Đã thực hiện xong.")
                artifacts = data.get("artifacts", [])

                print(f"  • Trạng thái tác vụ: {status}")
                print(f"  • Tóm tắt kết quả:   {summary}")
                print(f"  • Số Artifacts tạo: {len(artifacts)}")

                saved_files = []
                for art in artifacts:
                    art_id = art.get("artifact_id", "")
                    art_name = art.get("name") or f"{t['artifact_prefix']}_{art_id}.txt"
                    save_path = out_dir / art_name
                    if art_id:
                        print(f"    [*] Đang tải artifact '{art_name}' (ID: {art_id})...")
                        if download_artifact(t["url"], t["token"], art_id, save_path):
                            file_size = os.path.getsize(save_path)
                            print(f"    -> Đã lưu tại: {save_path} ({file_size} bytes)")
                            saved_files.append((art_name, f"{file_size} B", str(save_path)))
                        else:
                            # Lưu content trực tiếp nếu artifact có data
                            save_path.write_text(json.dumps(art, indent=2, ensure_ascii=False), encoding="utf-8")
                            saved_files.append((art_name, f"{os.path.getsize(save_path)} B", str(save_path)))

                summary_results.append({
                    "id": t["id"],
                    "role": t["role"],
                    "machine": t["machine"],
                    "action": t["action"],
                    "status": status,
                    "duration": f"{duration_s:.1f}s",
                    "artifacts": saved_files,
                    "summary": summary,
                })

        except Exception as e:
            duration_s = time.time() - t0
            print(f"\n❌ [LỖI DỪNG HỆ THỐNG] Ngoại lệ kết nối tới {t['id']} sau {duration_s:.1f}s: {e}")
            sys.exit(1)

    print("\n" + "=" * 115)
    print(" BẢNG TỔNG KẾT KẾT QUẢ KIỂM THỬ: TESTER & SECURITY (PC5 - 26.134.25.163)")
    print("=" * 115)
    print(f"| {'VAI TRÒ':<10} | {'AGENT ID':<13} | {'MÁY VẬT LÝ':<22} | {'ACTION':<20} | {'STATUS':<11} | {'THỜI GIAN':<10} | {'ARTIFACTS':<15} |")
    print("|" + "-" * 12 + "|" + "-" * 15 + "|" + "-" * 24 + "|" + "-" * 22 + "|" + "-" * 13 + "|" + "-" * 12 + "|" + "-" * 17 + "|")
    for r in summary_results:
        art_count_str = f"{len(r['artifacts'])} file(s)"
        print(f"| {r['role']:<10} | {r['id']:<13} | {r['machine']:<22} | {r['action']:<20} | {r['status']:<11} | {r['duration']:<10} | {art_count_str:<15} |")
    print("=" * 115)

    print("\n📂 Chi tiết các file kết quả đã tải về:")
    for r in summary_results:
        print(f"\n[+] {r['role']} ({r['id']}):")
        for fname, size, p in r["artifacts"]:
            print(f"    • File: {fname} ({size}) -> {p}")
            # Hiển thị trích đoạn nội dung file
            try:
                content = Path(p).read_text(encoding="utf-8")
                lines = content.strip().splitlines()
                preview = "\n      ".join(lines[:12])
                print(f"      --- Nội dung trích đoạn ---\n      {preview}\n      ...")
            except Exception:
                pass

    print("\n" + "=" * 115 + "\n")


if __name__ == "__main__":
    main()
