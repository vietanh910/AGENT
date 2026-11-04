#!/usr/bin/env python3
"""
scripts/lan_client.py
Client HTTP kiểm thử giao tiếp giữa các AI Agent qua mạng LAN theo Agent API v1.
Không phải Orchestrator, chỉ dùng để kiểm tra gọi API, đẩy/kéo artifact qua mạng.

Cách dùng:
    python scripts/lan_client.py health [agent_id|all]
    python scripts/lan_client.py task <agent_id> <task.json>
    python scripts/lan_client.py push-artifact <agent_id> <file> --type REQUIREMENT --created-by ba-01
    python scripts/lan_client.py pull-artifact <agent_id> <artifact_id> --out <thư_mục>
"""

import argparse
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

def _setup_console_encoding() -> None:
    if "pytest" in sys.modules:
        return
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

import httpx
import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_env_lan() -> None:
    """Tự động nạp biến môi trường từ .env.lan nếu có."""
    for p in [ROOT_DIR / ".env.lan", ROOT_DIR / "scripts" / ".env.lan"]:
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ.setdefault(k.strip(), v.strip())


def load_endpoints() -> dict[str, str]:
    """Đọc file scripts/endpoints.yaml, fallback sang endpoints.example.yaml nếu chưa tạo."""
    yaml_path = ROOT_DIR / "scripts" / "endpoints.yaml"
    if not yaml_path.exists():
        yaml_path = ROOT_DIR / "scripts" / "endpoints.example.yaml"

    if not yaml_path.exists():
        print(f"Lỗi: Không tìm thấy endpoints.yaml hoặc endpoints.example.yaml", file=sys.stderr)
        sys.exit(1)

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        print(f"Lỗi: File endpoints YAML không hợp lệ (phải là key-value dict).", file=sys.stderr)
        sys.exit(1)
    return {str(k): str(v).rstrip("/") for k, v in data.items()}


def get_token_for_agent(agent_id: str) -> str:
    """Lấy Bearer token cho agent từ biến môi trường AGENT_API_KEY_<AGENT_ID> hoặc AGENT_API_KEY."""
    load_env_lan()
    env_var_name = f"AGENT_API_KEY_{agent_id.upper().replace('-', '_')}"
    token = os.environ.get(env_var_name)
    if not token:
        token = os.environ.get("AGENT_API_KEY", "")
    return token.strip()


def cmd_health(agent_target: str) -> int:
    endpoints = load_endpoints()
    targets = endpoints.items() if agent_target.lower() == "all" else [(agent_target, endpoints.get(agent_target))]

    has_error = False
    for aid, base_url in targets:
        if not base_url:
            print(f"[FAIL] {aid}: Không tìm thấy endpoint trong endpoints.yaml", file=sys.stderr)
            has_error = True
            continue

        url = f"{base_url}/health"
        try:
            with httpx.Client(timeout=3.0) as client:
                res = client.get(url)
                if res.status_code == 200:
                    print(f"[PASS] {aid:16} ({url}): {res.json()}")
                else:
                    print(f"[FAIL] {aid:16} ({url}): HTTP {res.status_code} - {res.text}", file=sys.stderr)
                    has_error = True
        except Exception as e:
            print(f"[FAIL] {aid:16} ({url}): Không thể kết nối ({e})", file=sys.stderr)
            has_error = True

    return 1 if has_error else 0


def cmd_check_all() -> int:
    """Kiểm tra toàn bộ 10 Agent: /health, độ trễ và xác thực Bearer token."""
    import time
    endpoints = load_endpoints()
    print("=" * 95)
    print(" BẢNG KIỂM TRA 10 AGENT TRÊN 5 MÁY (RADMIN VPN / LAN)")
    print("=" * 95)
    print(f"| {'AGENT ID':16} | {'ENDPOINT':32} | {'HEALTH':12} | {'LATENCY':8} | {'XÁC THỰC TOKEN':16} |")
    print("|" + "-" * 18 + "|" + "-" * 34 + "|" + "-" * 14 + "|" + "-" * 10 + "|" + "-" * 18 + "|")

    for aid, base_url in endpoints.items():
        if not base_url:
            continue
        token = get_token_for_agent(aid)
        url_health = f"{base_url}/health"

        # 1. Health check
        health_status = "UNKNOWN"
        latency_str = "-"
        is_online = False
        try:
            t0 = time.time()
            with httpx.Client(timeout=3.0) as client:
                res = client.get(url_health)
                latency = int((time.time() - t0) * 1000)
                latency_str = f"{latency} ms"
                if res.status_code == 200:
                    health_status = "ONLINE"
                    is_online = True
                else:
                    health_status = f"HTTP {res.status_code}"
        except Exception:
            health_status = "OFFLINE"

        # 2. Auth check
        auth_status = "CHƯA BẬT"
        if is_online:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                with httpx.Client(timeout=3.0) as client:
                    res_agent = client.get(f"{base_url}/agent", headers=headers)
                    if res_agent.status_code == 200:
                        auth_status = "PASS (200)"
                    elif res_agent.status_code == 404:
                        res_status = client.get(f"{base_url}/status", headers=headers)
                        if res_status.status_code == 200:
                            auth_status = "PASS (200)"
                        elif res_status.status_code == 401:
                            auth_status = "FAIL (401)"
                        else:
                            auth_status = f"HTTP {res_status.status_code}"
                    elif res_agent.status_code == 401:
                        auth_status = "FAIL (401)"
                    else:
                        auth_status = f"HTTP {res_agent.status_code}"
            except Exception as e:
                auth_status = "ERROR"

        icon = "[+]" if health_status == "ONLINE" and "PASS" in auth_status else ("[-]" if health_status == "OFFLINE" else "[!]")
        print(f"| {icon} {aid:12} | {base_url:32} | {health_status:12} | {latency_str:8} | {auth_status:16} |")

    print("=" * 95)
    return 0


def cmd_task(agent_id: str, task_file_path: str) -> int:
    endpoints = load_endpoints()
    base_url = endpoints.get(agent_id)
    if not base_url:
        print(f"Lỗi: Không tìm thấy endpoint cho agent '{agent_id}' trong endpoints.yaml", file=sys.stderr)
        return 1

    path_obj = Path(task_file_path)
    if not path_obj.exists():
        print(f"Lỗi: Không tìm thấy file task tại '{task_file_path}'", file=sys.stderr)
        return 1

    with open(path_obj, "r", encoding="utf-8") as f:
        task_data = json.load(f)

    token = get_token_for_agent(agent_id)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    timeout = float(task_data.get("constraints", {}).get("timeout_seconds", 120))
    url = f"{base_url}/tasks"
    print(f"[*] Đang gửi task '{task_data.get('task_id')}' tới {agent_id} ({url})...")

    try:
        with httpx.Client(timeout=timeout + 5.0) as client:
            res = client.post(url, json=task_data, headers=headers)
            print(f"HTTP Status: {res.status_code}")
            try:
                print(json.dumps(res.json(), indent=2, ensure_ascii=False))
            except Exception:
                print(res.text)
            return 0 if res.status_code == 200 else 1
    except Exception as e:
        print(f"Lỗi kết nối tới {url}: {e}", file=sys.stderr)
        return 1


def cmd_push_artifact(agent_id: str, file_path_str: str, artifact_type: str, created_by: str) -> int:
    endpoints = load_endpoints()
    base_url = endpoints.get(agent_id)
    if not base_url:
        print(f"Lỗi: Không tìm thấy endpoint cho agent '{agent_id}' trong endpoints.yaml", file=sys.stderr)
        return 1

    file_path = Path(file_path_str)
    if not file_path.exists() or not file_path.is_file():
        print(f"Lỗi: Không tìm thấy file artifact tại '{file_path}'", file=sys.stderr)
        return 1

    with open(file_path, "rb") as f:
        content_bytes = f.read()

    checksum = hashlib.sha256(content_bytes).hexdigest()
    now_utc = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    metadata = {
        "artifact_id": f"ART-LAN-{datetime.now(timezone.utc).strftime('%H%M%S')}",
        "name": file_path.name,
        "type": artifact_type.upper(),
        "version": "1.0",
        "uri": f"artifacts/inbox/{created_by}/{file_path.name}",
        "created_by": created_by,
        "created_at": now_utc,
        "checksum": checksum,
    }

    token = get_token_for_agent(agent_id)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{base_url}/artifacts/inbox"
    print(f"[*] Đang đẩy artifact '{file_path.name}' (SHA256: {checksum[:8]}...) tới {agent_id} inbox ({url})...")

    files = {"file": (file_path.name, content_bytes, "application/octet-stream")}
    data = {"metadata": json.dumps(metadata)}

    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.post(url, data=data, files=files, headers=headers)
            print(f"HTTP Status: {res.status_code}")
            try:
                print(json.dumps(res.json(), indent=2, ensure_ascii=False))
            except Exception:
                print(res.text)
            return 0 if res.status_code == 200 else 1
    except Exception as e:
        print(f"Lỗi gửi artifact tới {url}: {e}", file=sys.stderr)
        return 1


def cmd_pull_artifact(agent_id: str, artifact_id: str, out_dir_str: str) -> int:
    endpoints = load_endpoints()
    base_url = endpoints.get(agent_id)
    if not base_url:
        print(f"Lỗi: Không tìm thấy endpoint cho agent '{agent_id}' trong endpoints.yaml", file=sys.stderr)
        return 1

    out_dir = Path(out_dir_str)
    out_dir.mkdir(parents=True, exist_ok=True)

    token = get_token_for_agent(agent_id)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{base_url}/artifacts/{artifact_id}/content"
    print(f"[*] Đang tải artifact '{artifact_id}' từ {agent_id} ({url})...")

    try:
        with httpx.Client(timeout=30.0) as client:
            res = client.get(url, headers=headers)
            if res.status_code != 200:
                print(f"[FAIL] HTTP {res.status_code}: {res.text}", file=sys.stderr)
                return 1

            expected_sha = res.headers.get("X-Checksum-SHA256")
            actual_sha = hashlib.sha256(res.content).hexdigest()

            if expected_sha and actual_sha.lower() != expected_sha.lower():
                print(f"[FAIL] Checksum SHA256 không khớp! Nhận: {actual_sha}, Header: {expected_sha}", file=sys.stderr)
                return 1

            # Lấy tên file từ header Content-Disposition nếu có
            cd = res.headers.get("Content-Disposition", "")
            filename = f"{artifact_id}.bin"
            if 'filename="' in cd:
                filename = cd.split('filename="')[1].split('"')[0]

            out_path = out_dir / filename
            with open(out_path, "wb") as f:
                f.write(res.content)

            print(f"[PASS] Đã tải thành công '{filename}' vào: {out_path.resolve()}")
            print(f"[*] Checksum SHA256 đã xác minh: {actual_sha}")
            return 0
    except Exception as e:
        print(f"Lỗi tải artifact từ {url}: {e}", file=sys.stderr)
        return 1


def main() -> None:
    _setup_console_encoding()
    parser = argparse.ArgumentParser(description="Client HTTP kiểm thử Agent qua mạng LAN (Agent API v1)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: check
    p_check = subparsers.add_parser("check", help="Kiểm tra toàn bộ 10 agent và bảng trạng thái xác thực token")

    # Subcommand: health
    p_health = subparsers.add_parser("health", help="Kiểm tra endpoint /health của agent")
    p_health.add_argument("agent_id", type=str, default="all", nargs="?", help="ID agent hoặc 'all'")

    # Subcommand: task
    p_task = subparsers.add_parser("task", help="Gửi TaskRequest JSON tới agent")
    p_task.add_argument("agent_id", type=str, help="ID agent nhận task")
    p_task.add_argument("task_file", type=str, help="Đường dẫn file task JSON")

    # Subcommand: push-artifact
    p_push = subparsers.add_parser("push-artifact", help="Đẩy một file artifact vào inbox của agent")
    p_push.add_argument("agent_id", type=str, help="ID agent nhận artifact")
    p_push.add_argument("file", type=str, help="Đường dẫn file cục bộ cần gửi")
    p_push.add_argument("--type", type=str, default="REQUIREMENT", help="Loại ArtifactType")
    p_push.add_argument("--created-by", type=str, default="pm-01", help="ID agent tạo ra artifact này")

    # Subcommand: pull-artifact
    p_pull = subparsers.add_parser("pull-artifact", help="Tải file artifact từ agent về thư mục chỉ định")
    p_pull.add_argument("agent_id", type=str, help="ID agent lưu giữ artifact")
    p_pull.add_argument("artifact_id", type=str, help="ID của artifact (ví dụ: ART-001)")
    p_pull.add_argument("--out", type=str, default="./downloads", help="Thư mục lưu file tải về")

    args = parser.parse_args()

    if args.command == "check":
        sys.exit(cmd_check_all())
    elif args.command == "health":
        sys.exit(cmd_health(args.agent_id))
    elif args.command == "task":
        sys.exit(cmd_task(args.agent_id, args.task_file))
    elif args.command == "push-artifact":
        sys.exit(cmd_push_artifact(args.agent_id, args.file, args.type, args.created_by))
    elif args.command == "pull-artifact":
        sys.exit(cmd_pull_artifact(args.agent_id, args.artifact_id, args.out))


if __name__ == "__main__":
    main()
