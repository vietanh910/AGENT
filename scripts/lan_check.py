#!/usr/bin/env python3
"""scripts/lan_check.py - Kiểm tra kết nối mạng LAN và tính tuân thủ của các Worker Host (Prompt S12).

Thực hiện từ PC1 Control Node đối với từng Worker Host (PC2 đến PC5):
1. Gọi GET /health: đo độ trễ mạng (latency ms), trạng thái hoạt động.
2. Kiểm tra độ lệch đồng hồ (clock skew): tính chênh lệch thời gian giữa PC1 và Worker, cảnh báo nếu lệch > 2.0s.
3. So khớp phiên bản: contract_version và worker_version.
4. Gọi GET /agents: xác thực danh sách agent phụ trách đúng theo bảng đăng ký.
5. Kiểm tra bảo mật: gọi GET /agents không có Bearer token -> bắt buộc nhận HTTP 401 Unauthorized.
6. Xuất bảng kết quả PASS/FAIL ra terminal và tạo báo cáo Markdown tại docs/lan-check-report.md.

Cách sử dụng:
    python scripts/lan_check.py
    python scripts/lan_check.py --mock  # Dựng mock worker cục bộ để kiểm tra tự động
    python scripts/lan_check.py --config control/workers.yaml
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import httpx
import yaml
from agent_contract.models import CONTRACT_VERSION

def _setup_console_encoding() -> None:
    if "pytest" in sys.modules:
        return
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

REPORT_FILE = ROOT_DIR / "docs" / "lan-check-report.md"

DEFAULT_WORKER_ENDPOINTS = {
    "worker-pc2": {
        "machine_id": "PC2",
        "url": "http://26.28.90.248:8001",
        "secret": "worker_pc2_secret_token_change_me",
        "expected_agents": ["dev-01", "scrum_master-01"],
    },
    "worker-pc3": {
        "machine_id": "PC3",
        "url": "http://26.53.120.228:8001",
        "secret": "worker_pc3_secret_token_change_me",
        "expected_agents": ["po-01", "sa-01"],
    },
    "worker-pc4": {
        "machine_id": "PC4",
        "url": "http://26.222.181.101:8001",
        "secret": "worker_pc4_secret_token_change_me",
        "expected_agents": ["ba-01", "dba-01"],
    },
    "worker-pc5": {
        "machine_id": "PC5",
        "url": "http://26.134.25.163:8001",
        "secret": "worker_pc5_secret_token_change_me",
        "expected_agents": ["tester-01", "security-01"],
    },
}


def load_worker_configs(workers_yaml_path: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Nạp danh sách worker và secret từ control/workers.yaml hoặc dùng cấu hình mặc định."""
    target_path = workers_yaml_path or (ROOT_DIR / "control" / "workers.yaml")
    result = dict(DEFAULT_WORKER_ENDPOINTS)

    if target_path.is_file():
        try:
            with open(target_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            workers = data.get("workers", {})
            for wid, wcfg in workers.items():
                if wid in result:
                    result[wid]["secret"] = wcfg.get("secret", result[wid]["secret"])
                    result[wid]["machine_id"] = wcfg.get("machine_id", result[wid]["machine_id"])
                    if "allowed_agents" in wcfg:
                        result[wid]["expected_agents"] = wcfg["allowed_agents"]
                    if "base_url" in wcfg:
                        result[wid]["url"] = wcfg["base_url"]
        except Exception as e:
            print(f"[!] Cảnh báo: Không thể nạp {target_path}: {e}")

    return result


def parse_remote_time(date_str: Optional[str]) -> Optional[datetime]:
    """Phân tích thời gian trả về từ /health (ISO 8601 hoặc RFC 2822)."""
    if not date_str:
        return None
    try:
        # Thử ISO format
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(date_str)
    except Exception:
        return None


def check_worker(
    worker_id: str,
    info: Dict[str, Any],
    client: Optional[httpx.Client] = None,
) -> Dict[str, Any]:
    """Kiểm tra toàn bộ 5 tiêu chí của một Worker Host."""
    url = info["url"].rstrip("/")
    secret = info["secret"]
    machine_id = info["machine_id"]
    expected_agents = set(info["expected_agents"])

    row: Dict[str, Any] = {
        "worker_id": worker_id,
        "machine_id": machine_id,
        "url": url,
        "health_pass": False,
        "auth_401_pass": False,
        "agents_pass": False,
        "latency_ms": 0,
        "clock_skew_seconds": 0.0,
        "contract_version": "N/A",
        "worker_version": "N/A",
        "agents_found": [],
        "overall_pass": False,
        "error_message": "",
    }

    own_client = client is None
    c = client or httpx.Client(timeout=4.0)

    try:
        # 1. Gọi GET /health với Bearer token
        t0 = time.perf_counter()
        headers = {"Authorization": f"Bearer {secret}"}
        res_health = c.get(f"{url}/health", headers=headers)
        t1 = time.perf_counter()
        row["latency_ms"] = int((t1 - t0) * 1000)

        now_utc = datetime.now(timezone.utc)

        if res_health.status_code == 200:
            h_data = res_health.json()
            row["contract_version"] = h_data.get("contract_version", "unknown")
            row["worker_version"] = h_data.get("version", "unknown")

            # Tính clock skew
            remote_ts_str = h_data.get("timestamp") or res_health.headers.get("Date")
            remote_dt = parse_remote_time(remote_ts_str)
            if remote_dt:
                skew = abs((now_utc - remote_dt).total_seconds())
                row["clock_skew_seconds"] = round(skew, 3)

            # Đánh giá tiêu chí /health
            if row["contract_version"] == CONTRACT_VERSION and row["clock_skew_seconds"] <= 2.0:
                row["health_pass"] = True
            else:
                reasons = []
                if row["contract_version"] != CONTRACT_VERSION:
                    reasons.append(f"contract_version={row['contract_version']} != {CONTRACT_VERSION}")
                if row["clock_skew_seconds"] > 2.0:
                    reasons.append(f"clock_skew={row['clock_skew_seconds']}s > 2.0s")
                row["error_message"] = "; ".join(reasons)
        else:
            row["error_message"] = f"/health trả HTTP {res_health.status_code}"

        # 2. Gọi GET /agents với Bearer token
        res_agents = c.get(f"{url}/agents", headers=headers)
        if res_agents.status_code == 200:
            agents_list = res_agents.json()
            found_ids = {a.get("agent_id") for a in agents_list}
            row["agents_found"] = list(found_ids)
            if expected_agents.issubset(found_ids):
                row["agents_pass"] = True
            else:
                row["error_message"] += f" | Thiếu agent: mong đợi {expected_agents}, nhận {found_ids}"
        else:
            row["error_message"] += f" | /agents trả HTTP {res_agents.status_code}"

        # 3. Thử gọi GET /agents KHÔNG có token -> Bắt buộc nhận 401
        res_no_auth = c.get(f"{url}/agents", headers={})
        if res_no_auth.status_code == 401:
            row["auth_401_pass"] = True
        else:
            row["error_message"] += f" | Không có token trả HTTP {res_no_auth.status_code} (kỳ vọng 401)"

        # Tổng hợp kết quả
        row["overall_pass"] = (
            row["health_pass"]
            and row["auth_401_pass"]
            and row["agents_pass"]
            and row["latency_ms"] < 2000
        )

    except Exception as e:
        row["error_message"] = f"Lỗi kết nối tới {url}: {e}"
        row["overall_pass"] = False
    finally:
        if own_client:
            c.close()

    return row


def generate_markdown_report(results: List[Dict[str, Any]], filepath: Path) -> None:
    """Tạo báo cáo Markdown chi tiết tại docs/lan-check-report.md."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    all_passed = all(r["overall_pass"] for r in results)

    content = f"""# Báo cáo Kiểm tra Kết nối Mạng LAN & Worker Host (Prompt S12)

*Thời gian thực hiện kiểm tra: {now_str}*
*Địa điểm thực thi: PC1 Control Node*
*Phiên bản Agent Contract chuẩn: `{CONTRACT_VERSION}`*

---

## 1. Kết quả Tổng quan

**Trạng thái kiểm tra toàn hệ thống**: **{'🟢 TOÀN BỘ PASS' if all_passed else '🔴 PHÁT HIỆN LỖI (FAIL)'}**

| Worker ID | Máy | Địa chỉ URL | Độ trễ (ms) | Lệch đồng hồ | Contract | Worker Ver | Agents | Auth 401 | Đánh giá |
|---|:---:|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
"""
    for r in results:
        h_icon = "PASS" if r["health_pass"] else "FAIL"
        a_icon = "PASS" if r["agents_pass"] else "FAIL"
        sec_icon = "PASS" if r["auth_401_pass"] else "FAIL"
        overall = "✅ **PASS**" if r["overall_pass"] else "❌ **FAIL**"
        content += (
            f"| `{r['worker_id']}` | **{r['machine_id']}** | `{r['url']}` "
            f"| `{r['latency_ms']}ms` | `{r['clock_skew_seconds']}s` "
            f"| `{r['contract_version']}` | `{r['worker_version']}` "
            f"| {a_icon} ({len(r['agents_found'])}) | {sec_icon} | {overall} |\n"
        )

    content += """
---

## 2. Chi tiết Từng Tiêu chí Kiểm định

1. **Độ trễ Mạng (Latency)**: Đo thời gian khứ hồi (RTT) qua giao thức HTTP. Yêu cầu < 100ms trong mạng LAN.
2. **Độ lệch Thời gian (Clock Skew)**: So sánh đồng hồ nội bộ giữa PC1 và từng Worker. Ngưỡng cảnh báo: $\\Delta t > 2.0s$.
3. **Tính Tuân thủ Phiên bản Contract**: Đảm bảo tất cả Worker đồng nhất phiên bản `""" + CONTRACT_VERSION + """` để tránh xung đột cấu trúc schema dữ liệu.
4. **Đăng ký Agent Đúng Vai trò**:
   - PC2: `dev-01` (DEV), `scrum_master-01` (SCRUM_MASTER)
   - PC3: `po-01` (PO), `sa-01` (SA)
   - PC4: `ba-01` (BA), `dba-01` (DBA)
   - PC5: `tester-01` (TESTER), `security-01` (SECURITY)
5. **Kiểm tra Xác thực Token (Security Probe)**: Gọi endpoint quản trị không mang Bearer token bắt buộc nhận mã trạng thái `HTTP 401 Unauthorized`.

---

## 3. Gợi ý Khắc phục Sự cố (Troubleshooting)

- **Worker trả về Lỗi Kết nối**:
  - Kiểm tra xem Worker Host đã được khởi chạy chưa: `python scripts/start_worker.py --config worker/worker.<pc>.yaml`.
  - Kiểm tra firewall trên máy đích có mở cổng 8000 cho dải IP LAN không.
- **Clock Skew quá 2.0s**:
  - Chạy lệnh đồng bộ thời gian NTP trên máy worker: `w32tm /resync` (Windows) hoặc `sudo chronyc makestep` (Linux).
- **Lỗi 401 khi có Token**:
  - Kiểm tra giá trị `secret` trong `control/workers.yaml` trên PC1 có khớp 100% với file `worker.yaml` trên Worker không.
"""

    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


def run_mock_lan_check() -> Tuple[List[Dict[str, Any]], bool]:
    """Chạy kiểm tra mô phỏng với FastAPI TestClient cho toàn bộ 4 Worker cục bộ."""
    from fastapi.testclient import TestClient
    from worker.fake_agent import FakeAgent
    from worker.host.api import create_worker_api
    from worker.host.config import WorkerAgentConfig, WorkerHostConfig
    from worker.host.outbox import OutboxService
    from worker.host.runner import RunnerRegistry

    print(">> Đang khởi tạo mock app cho 4 Worker (PC2..PC5)...")
    results = []

    configs = [
        ("worker-pc2", "PC2", "worker_pc2_secret_token_change_me", [("dev-01", "DEV"), ("scrum_master-01", "SCRUM_MASTER")]),
        ("worker-pc3", "PC3", "worker_pc3_secret_token_change_me", [("po-01", "PO"), ("sa-01", "SA")]),
        ("worker-pc4", "PC4", "worker_pc4_secret_token_change_me", [("ba-01", "BA"), ("dba-01", "DBA")]),
        ("worker-pc5", "PC5", "worker_pc5_secret_token_change_me", [("tester-01", "TESTER"), ("security-01", "SECURITY")]),
    ]

    for wid, mid, secret, agents_info in configs:
        agent_cfgs = [
            WorkerAgentConfig(agent_id=aid, role=role, name=f"Agent {aid}")
            for aid, role in agents_info
        ]
        w_cfg = WorkerHostConfig(
            worker_id=wid,
            machine_id=mid,
            secret=secret,
            contract_version=CONTRACT_VERSION,
            worker_version="1.0.0",
            outbox_db_path=f"worker/data/test_outbox_{wid}.db",
            agents=agent_cfgs,
        )
        outbox = OutboxService(db_path=w_cfg.outbox_db_path)
        runner = RunnerRegistry(outbox=outbox, secret=secret)
        for aid, role in agents_info:
            from agent_contract.enums import AgentRole
            runner.register_runner(aid, AgentRole(role), FakeAgent(aid, AgentRole(role)))

        app = create_worker_api(w_cfg, runner, outbox)
        client = TestClient(app)

        info = {
            "machine_id": mid,
            "url": f"http://mock-{wid}",
            "secret": secret,
            "expected_agents": [aid for aid, _ in agents_info],
        }

        # Kiểm tra qua client
        row = check_worker(wid, info, client=client)  # type: ignore[arg-type]
        results.append(row)

    return results, all(r["overall_pass"] for r in results)


def print_table(results: List[Dict[str, Any]]) -> None:
    """In bảng ASCII kết quả ra terminal."""
    print("=" * 105)
    print(" KIỂM TRA MẠNG LAN VÀ TÍNH HỢP LỆ WORKER HOST (Prompt S12)")
    print("=" * 105)
    print(
        f"| {'WORKER':<12} | {'MÁY':<5} | {'LATENCY':<8} | {'CLOCK SKEW':<11} | "
        f"{'HEALTH':<8} | {'AGENTS':<8} | {'AUTH 401':<9} | {'KẾT QUẢ':<8} |"
    )
    print("|" + "-" * 14 + "|" + "-" * 7 + "|" + "-" * 10 + "|" + "-" * 13 + "|" + "-" * 10 + "|" + "-" * 10 + "|" + "-" * 11 + "|" + "-" * 10 + "|")

    for r in results:
        h_str = "PASS" if r["health_pass"] else "FAIL"
        a_str = "PASS" if r["agents_pass"] else "FAIL"
        s_str = "PASS" if r["auth_401_pass"] else "FAIL"
        res_str = "PASS" if r["overall_pass"] else "FAIL"
        print(
            f"| {r['worker_id']:<12} | {r['machine_id']:<5} | {r['latency_ms']:<6}ms | "
            f"{r['clock_skew_seconds']:<9}s | {h_str:<8} | {a_str:<8} | {s_str:<9} | {res_str:<8} |"
        )
        if r["error_message"]:
            print(f"  👉 Ghi chú: {r['error_message']}")

    print("=" * 105)


def run_legacy_endpoints_check(endpoints_path: Path) -> Tuple[List[Dict[str, Any]], bool]:
    """Kiểm tra tương thích ngược theo Prompt 7 khi có file scripts/endpoints.yaml."""
    with open(endpoints_path, "r", encoding="utf-8") as f:
        endpoints = yaml.safe_load(f) or {}

    results = []
    contract_versions = {}
    has_failure = False

    for agent_id, base_url in endpoints.items():
        base_url = str(base_url).rstrip("/")
        row = {
            "worker_id": agent_id,
            "machine_id": "LAN",
            "url": base_url,
            "health_pass": False,
            "auth_401_pass": False,
            "agents_pass": True,
            "latency_ms": 0,
            "clock_skew_seconds": 0.0,
            "contract_version": "N/A",
            "worker_version": "N/A",
            "agents_found": [agent_id],
            "overall_pass": False,
            "error_message": "",
        }

        # 1. Gọi GET /health
        start = time.perf_counter()
        try:
            with httpx.Client(timeout=4.0) as client:
                res_health = client.get(f"{base_url}/health")
                row["latency_ms"] = int((time.perf_counter() - start) * 1000)
                if res_health.status_code == 200:
                    h_json = res_health.json()
                    row["contract_version"] = h_json.get("contract_version", "unknown")
                    contract_versions[agent_id] = row["contract_version"]
                    row["health_pass"] = True
                else:
                    row["error_message"] = f"/health trả HTTP {res_health.status_code}"
                    has_failure = True
        except Exception as e:
            row["latency_ms"] = int((time.perf_counter() - start) * 1000)
            row["error_message"] = f"Lỗi: {e}"
            has_failure = True

        # 2. Gọi GET /agent không có token -> phải nhận 401
        try:
            with httpx.Client(timeout=4.0) as client:
                res_auth = client.get(f"{base_url}/agent")
                if res_auth.status_code == 401:
                    row["auth_401_pass"] = True
                else:
                    row["error_message"] += f" | /agent trả HTTP {res_auth.status_code} (kỳ vọng 401)"
                    has_failure = True
        except Exception as e:
            row["error_message"] += f" | Lỗi /agent: {e}"
            has_failure = True

        row["overall_pass"] = row["health_pass"] and row["auth_401_pass"]
        results.append(row)

    distinct_cvers = set(contract_versions.values())
    cver_consistent = len(distinct_cvers) == 1

    # Xuất báo cáo Markdown tương thích Prompt 7
    report_lines = [
        "# Báo cáo Kiểm tra Kết nối Mạng LAN (Agent API v1)",
        f"\n*Thời gian thực thi: {datetime.now(timezone.utc).isoformat()}*",
        "\n## 1. Kết quả Chi tiết",
        "\n| AGENT ID | ENDPOINT | HEALTH | AUTH 401 | ĐỘ TRỄ | VERSION |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        h_str = "PASS" if r["health_pass"] else "FAIL"
        a_str = "PASS" if r["auth_401_pass"] else "FAIL"
        report_lines.append(f"| `{r['worker_id']}` | `{r['url']}` | {h_str} | {a_str} | {r['latency_ms']}ms | `{r['contract_version']}` |")

    report_lines.append("\n## 2. Đồng bộ Phiên bản")
    if cver_consistent:
        report_lines.append(f"- Tính đồng nhất contract_version: **ĐỒNG NHẤT** (`{list(distinct_cvers)[0]}`)")
    else:
        report_lines.append(f"- Tính đồng nhất contract_version: **KHÔNG ĐỒNG NHẤT** ({distinct_cvers})")

    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(report_lines), encoding="utf-8")

    return results, not has_failure and cver_consistent


def main(argv: Optional[List[str]] = None) -> int:
    _setup_console_encoding()
    parser = argparse.ArgumentParser(description="Kiểm tra kết nối mạng LAN và Worker Hosts (PC1 -> PC2..PC5).")
    parser.add_argument("--config", type=str, default=None, help="Đường dẫn file workers.yaml.")
    parser.add_argument("--mock", action="store_true", help="Chạy kiểm tra mô phỏng mock workers (dùng cho test/CI).")

    if argv is None and "pytest" in sys.modules:
        args, _ = parser.parse_known_args([])
    else:
        args, _ = parser.parse_known_args(argv)

    endpoints_yaml = ROOT_DIR / "scripts" / "endpoints.yaml"
    if endpoints_yaml.is_file() and not args.mock and not args.config:
        results, all_passed = run_legacy_endpoints_check(endpoints_yaml)
    elif args.mock:
        results, all_passed = run_mock_lan_check()
    else:
        configs = load_worker_configs(Path(args.config) if args.config else None)
        results = []
        for wid, info in configs.items():
            r = check_worker(wid, info)
            results.append(r)
        all_passed = all(r["overall_pass"] for r in results)
        generate_markdown_report(results, REPORT_FILE)

    print_table(results)
    print(f"\n[+] Đã xuất báo cáo chi tiết: {REPORT_FILE}")

    if all_passed:
        print("[+] KẾT QUẢ: Kiểm tra mạng LAN đạt chuẩn 100% PASS.")
        return 0
    else:
        print("[!] KẾT QUẢ: Phát hiện lỗi kết nối. Vui lòng kiểm tra báo cáo.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
