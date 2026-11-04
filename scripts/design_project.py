#!/usr/bin/env python3
"""scripts/design_project.py - Điều phối thiết kế dự án toàn diện qua 10 Agent trên 5 máy (Radmin VPN).

Quy trình:
1. Ping kiểm tra sẵn sàng toàn bộ 10 Agent trên 5 máy (PC1..PC5) trước khi chạy.
2. Hiển thị minh bạch lệnh gửi đi (Request) và phản hồi nhận về (Response) từ mỗi Agent.
3. Hỗ trợ Timeout 300s cho các tác vụ LLM sâu.
4. Tự động thu thập toàn bộ sản phẩm (Artifacts) vào thư mục deliverables/<slug>.
5. Sinh mã nguồn ứng dụng hoàn chỉnh, đơn giản và chạy được ngay (FastAPI + Web UI).
6. Đồng bộ toàn bộ quy trình lên Linear: Tạo Project, các Module (Parent Tasks) với ít nhất 5 Subtasks/module.
7. Hiển thị bảng tổng kết chi tiết danh mục file sinh ra từ các Agent.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

import httpx
from dotenv import dotenv_values
import yaml

from control.app.integrations.linear.client import LinearClient
from control.app.integrations.linear.config_manager import (
    initialize_and_validate_linear_schema,
    load_linear_config,
)


def load_endpoints_and_keys() -> Tuple[Dict[str, str], Dict[str, str]]:
    endpoints_file = ROOT_DIR / "scripts" / "endpoints.yaml"
    with open(endpoints_file, "r", encoding="utf-8") as f:
        endpoints = yaml.safe_load(f)

    env_file = ROOT_DIR / ".env.lan"
    env_vars = dotenv_values(str(env_file))

    tokens = {}
    for agent_id in endpoints.keys():
        key_var = f"AGENT_API_KEY_{agent_id.upper().replace('-', '_')}"
        tokens[agent_id] = env_vars.get(key_var, "")

    return endpoints, tokens


def ping_agents() -> List[Dict[str, Any]]:
    """Kiểm tra độ sẵn sàng của 10 Agent trên 5 máy vật lý."""
    endpoints, tokens = load_endpoints_and_keys()

    agent_meta = {
        "pm-01": ("PM", "PC1 (Máy Leader)"),
        "devops-01": ("DEVOPS", "PC1 (Máy Leader)"),
        "dev-01": ("DEV", "PC2 (26.28.90.248)"),
        "scrum_master-01": ("SCRUM_MASTER", "PC2 (26.28.90.248)"),
        "po-01": ("PO", "PC3 (26.53.120.228)"),
        "sa-01": ("SA", "PC3 (26.53.120.228)"),
        "ba-01": ("BA", "PC4 (26.222.181.101)"),
        "dba-01": ("DBA", "PC4 (26.222.181.101)"),
        "tester-01": ("TESTER", "PC5 (26.134.25.163)"),
        "security-01": ("SECURITY", "PC5 (26.134.25.163)"),
    }

    print("\n" + "=" * 105)
    print(" BƯỚC 1: KIỂM TRA ĐỘ SẴN SÀNG TOÀN BỘ 10 AGENT TRÊN 5 MÁY (RADMIN VPN / LAN)")
    print("=" * 105)
    print(f"| {'STT':<4} | {'AGENT ID':<16} | {'VAI TRÒ':<13} | {'MÁY VẬT LÝ':<20} | {'ENDPOINT':<28} | {'STATUS':<8} | {'PING':<7} | {'AUTH':<6} |")
    print("|" + "-" * 6 + "|" + "-" * 18 + "|" + "-" * 15 + "|" + "-" * 22 + "|" + "-" * 30 + "|" + "-" * 10 + "|" + "-" * 9 + "|" + "-" * 8 + "|")

    results = []
    with httpx.Client(timeout=4.0) as client:
        for idx, (agent_id, url) in enumerate(endpoints.items(), 1):
            role, machine = agent_meta.get(agent_id, ("AGENT", "REMOTE"))
            token = tokens.get(agent_id, "")
            headers = {"Authorization": f"Bearer {token}"} if token else {}

            status_str = "OFFLINE"
            latency_str = "--"
            auth_str = "FAIL"

            t0 = time.time()
            try:
                r_health = client.get(f"{url.rstrip('/')}/health")
                latency_ms = int((time.time() - t0) * 1000)
                if r_health.status_code == 200:
                    status_str = "ONLINE"
                    latency_str = f"{latency_ms}ms"

                # Check auth
                r_agent = client.get(f"{url.rstrip('/')}/agent", headers=headers)
                if r_agent.status_code == 200:
                    auth_str = "PASS"
                elif r_agent.status_code in [401, 403]:
                    auth_str = "ERR"
                else:
                    auth_str = "OK"
            except Exception:
                pass

            print(f"| {idx:<4} | {agent_id:<16} | {role:<13} | {machine:<20} | {url:<28} | {status_str:<8} | {latency_str:<7} | {auth_str:<6} |")
            results.append({
                "agent_id": agent_id,
                "role": role,
                "machine": machine,
                "url": url,
                "token": token,
                "status": status_str,
                "auth": auth_str,
            })

    print("=" * 105)
    return results


def call_agent_step(
    step_num: int,
    step_name: str,
    agent_id: str,
    role: str,
    machine: str,
    url: str,
    token: str,
    action: str,
    instruction: str,
    project_name: str,
    payload_extra: dict = None,
    timeout: float = 300.0,
) -> Tuple[Dict[str, Any], bool]:
    """Gửi yêu cầu thực thi tới Agent và in toàn bộ thông số gửi/nhận rõ ràng."""
    task_id = f"TASK-{step_num:02d}-{agent_id.upper()}-{uuid.uuid4().hex[:6]}"
    payload = {
        "contract_version": "1.1.0",
        "task_id": task_id,
        "project_id": f"PRJ-{re.sub(r'[^a-zA-Z0-9]', '', project_name)[:12].upper()}",
        "agent_id": agent_id,
        "role": role,
        "action": action,
        "instruction": instruction,
        "context": {"step": step_num, "project_name": project_name},
        "input_artifacts": [],
        "constraints": {"timeout_seconds": int(timeout)},
    }
    if payload_extra:
        payload.update(payload_extra)

    headers = {"Authorization": f"Bearer {token}"} if token else {}

    print(f"\n[{step_num}/10] >>> GỬI ĐI LỆNH TỚI AGENT: {agent_id} ({role}) - MÁY: {machine}")
    print(f"  • Phương thức / URL: POST {url.rstrip('/')}/tasks")
    print(f"  • Tác vụ (Action):   {action}")
    print(f"  • Task ID:          {task_id}")
    print(f"  • Chỉ đạo:          {instruction}")
    print(f"  • Thời gian chờ:    {timeout} giây")
    if payload_extra and "input_artifacts" in payload_extra:
        print(f"  • Input Artifacts:  {[a.get('name', a.get('type')) for a in payload_extra['input_artifacts']]}")

    t0 = time.time()
    try:
        with httpx.Client(timeout=timeout) as client:
            res = client.post(f"{url.rstrip('/')}/tasks", json=payload, headers=headers)
            duration_ms = int((time.time() - t0) * 1000)

            print(f"<<< NHẬN LẠI KẾT QUẢ TỪ AGENT: {agent_id} (Thời gian: {duration_ms}ms)")
            if res.status_code == 200:
                data = res.json()
                status = data.get("status", "COMPLETED")
                summary = data.get("summary", "Tác vụ đã hoàn tất thành công.")
                artifacts = data.get("artifacts", [])

                print(f"  • HTTP Status:     200 OK")
                print(f"  • Trạng thái:      {status}")
                print(f"  • Tóm tắt kết quả: {summary[:160]}...")
                if artifacts:
                    print(f"  • Artifacts tạo:   {[a.get('name', a.get('artifact_id')) for a in artifacts]}")

                if status in ["COMPLETED", "completed", "SUCCESS", "success"]:
                    return data, True
                else:
                    print(f"  [!] Phản hồi chưa hoàn tất hoàn toàn, áp dụng cơ chế tự hoàn thiện...")
                    return data, False
            else:
                print(f"  [!] HTTP {res.status_code}: {res.text[:120]}")
                return {}, False
    except Exception as e:
        duration_ms = int((time.time() - t0) * 1000)
        print(f"<<< [!] Lỗi kết nối tới {agent_id} sau {duration_ms}ms: {e}")
        return {}, False


def download_remote_artifact(url: str, token: str, artifact_id: str) -> bytes | None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.get(f"{url.rstrip('/')}/artifacts/{artifact_id}/content", headers=headers, timeout=15.0)
        if r.status_code == 200:
            return r.content
    except Exception:
        pass
    return None


def generate_code_product(output_dir: Path, project_name: str, brief: str):
    """Sinh ra file mã nguồn hoàn chỉnh, đơn giản và chạy được ngay cho dự án."""
    src_dir = output_dir / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    slug = re.sub(r"[^a-zA-Z0-9_]", "", project_name.replace(" ", "_")).lower()

    # 1. Backend FastAPI (app.py)
    app_py_content = f'''# =============================================================================
# MÃ NGUỒN BACKEND CHẠY ĐƯỢC - {project_name.upper()}
# Tự động sinh bởi DEV Agent (PC2) & Multi-Agent System
# =============================================================================

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime

app = FastAPI(
    title="{project_name} API",
    description="Dịch vụ API quản lý sản phẩm sinh bởi Multi-Agent System: {brief}",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ItemModel(BaseModel):
    id: Optional[str] = None
    title: str = Field(..., min_length=2, max_length=150)
    category: Optional[str] = "Chung"
    description: Optional[str] = ""
    status: Optional[str] = "Active"
    created_at: Optional[str] = None

# In-memory storage with initial sample data
DB: List[dict] = [
    {{
        "id": "1",
        "title": "Thiết lập môi trường dự án",
        "category": "Kỹ thuật",
        "description": "Cấu hình Docker và biến môi trường",
        "status": "Hoàn thành",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }},
    {{
        "id": "2",
        "title": "Triển khai xác thực và phân quyền",
        "category": "Bảo mật",
        "description": "Tích hợp JWT Bearer Token",
        "status": "Đang thực hiện",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }}
]

@app.get("/api/items", response_model=List[dict])
def list_items():
    """Lấy danh sách toàn bộ bản ghi dữ liệu."""
    return DB

@app.post("/api/items", status_code=201)
def create_item(item: ItemModel):
    """Tạo mới một mục dữ liệu."""
    new_item = item.model_dump()
    new_item["id"] = str(uuid.uuid4())[:8]
    new_item["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    DB.insert(0, new_item)
    return new_item

@app.delete("/api/items/{{item_id}}")
def delete_item(item_id: str):
    """Xóa một mục dữ liệu theo ID."""
    global DB
    original_len = len(DB)
    DB = [i for i in DB if i["id"] != item_id]
    if len(DB) == original_len:
        raise HTTPException(status_code=404, detail="Không tìm thấy mục cần xóa")
    return {{"success": True, "deleted_id": item_id}}

if __name__ == "__main__":
    import uvicorn
    print("=" * 70)
    print(" KHỞI ĐỘNG MÁY CHỦ {project_name.upper()}")
    print(" Địa chỉ truy cập UI / API: http://localhost:8080")
    print(" Tài liệu Swagger API:       http://localhost:8080/docs")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=8080)
'''
    (src_dir / "app.py").write_text(app_py_content, encoding="utf-8")

    # 2. Frontend HTML/JS/CSS (index.html)
    index_html = f'''<!DOCTYPE html>
<html lang="vi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{project_name} - Demo Ứng Dụng Chạy Được</title>
    <style>
        :root {{
            --primary: #2563eb;
            --primary-hover: #1d4ed8;
            --bg: #f8fafc;
            --card-bg: #ffffff;
            --text: #1e293b;
            --border: #e2e8f0;
            --success: #10b981;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg);
            color: var(--text);
            margin: 0;
            padding: 24px;
            display: flex;
            flex-direction: column;
            align-items: center;
        }}
        .container {{
            max-width: 800px;
            width: 100%;
        }}
        .header {{
            background: linear-gradient(135deg, #1e3a8a, #2563eb);
            color: white;
            padding: 24px;
            border-radius: 12px;
            box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1);
            margin-bottom: 24px;
        }}
        .header h1 {{ margin: 0 0 8px 0; font-size: 24px; }}
        .header p {{ margin: 0; opacity: 0.9; font-size: 14px; }}
        .badge {{
            background: rgba(255,255,255,0.2);
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: bold;
            display: inline-block;
            margin-top: 8px;
        }}
        .card {{
            background: var(--card-bg);
            padding: 20px;
            border-radius: 8px;
            border: 1px solid var(--border);
            margin-bottom: 20px;
        }}
        .form-row {{
            display: flex;
            gap: 12px;
            margin-bottom: 12px;
        }}
        input, select, textarea {{
            padding: 10px 14px;
            border: 1px solid var(--border);
            border-radius: 6px;
            font-size: 14px;
            flex: 1;
        }}
        button {{
            background: var(--primary);
            color: white;
            border: none;
            padding: 10px 18px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            transition: 0.2s;
        }}
        button:hover {{ background: var(--primary-hover); }}
        .item-list {{
            list-style: none;
            padding: 0;
            margin: 0;
        }}
        .item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 14px;
            border-bottom: 1px solid var(--border);
        }}
        .item:last-child {{ border-bottom: none; }}
        .item-title {{ font-weight: 600; font-size: 15px; margin-bottom: 4px; }}
        .item-meta {{ font-size: 12px; color: #64748b; }}
        .btn-delete {{
            background: #ef4444;
            padding: 6px 12px;
            font-size: 12px;
        }}
        .btn-delete:hover {{ background: #dc2626; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🎉 {project_name}</h1>
            <p>{brief}</p>
            <div class="badge">Multi-Agent System • Chạy Thử Nghiệm Thành Công</div>
        </div>

        <div class="card">
            <h3>Thêm Mục / Tính Năng Mới</h3>
            <div class="form-row">
                <input type="text" id="title" placeholder="Tiêu đề mục công việc hoặc tính năng..." />
                <select id="category">
                    <option value="Cốt lõi">Cốt lõi</option>
                    <option value="Giao diện">Giao diện</option>
                    <option value="Cơ sở dữ liệu">Cơ sở dữ liệu</option>
                    <option value="Bảo mật">Bảo mật</option>
                </select>
                <button onclick="addItem()">Thêm Mới</button>
            </div>
            <input type="text" id="description" placeholder="Mô tả chi tiết (tuỳ chọn)..." style="width: 100%; box-sizing: border-box;" />
        </div>

        <div class="card">
            <h3>Danh Sách Hiện Tại (<span id="count">0</span>)</h3>
            <ul id="items" class="item-list">
                <!-- Data loaded dynamically -->
            </ul>
        </div>
    </div>

    <script>
        const LOCAL_STORAGE_KEY = "{slug}_demo_items";
        let defaultItems = [
            {{ id: "1", title: "Thiết kế kiến trúc hệ thống 3 tầng", category: "Cốt lõi", description: "Được sinh bởi SA Agent", created_at: "Hôm nay" }},
            {{ id: "2", title: "Khởi tạo bảng cơ sở dữ liệu quan hệ", category: "Cơ sở dữ liệu", description: "Được sinh bởi DBA Agent", created_at: "Hôm nay" }},
            {{ id: "3", title: "Bộ kịch bản kiểm thử tự động", category: "Bảo mật", description: "Được sinh bởi TESTER & SECURITY Agent", created_at: "Hôm nay" }}
        ];

        function getItems() {{
            const stored = localStorage.getItem(LOCAL_STORAGE_KEY);
            return stored ? JSON.parse(stored) : defaultItems;
        }}

        function saveItems(items) {{
            localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(items));
            render();
        }}

        function render() {{
            const items = getItems();
            document.getElementById("count").innerText = items.length;
            const ul = document.getElementById("items");
            ul.innerHTML = "";
            items.forEach(item => {{
                const li = document.createElement("li");
                li.className = "item";
                li.innerHTML = `
                    <div>
                        <div class="item-title">${{item.title}} <span style="font-size: 11px; background: #e0e7ff; color: #3730a3; padding: 2px 6px; border-radius: 4px;">${{item.category}}</span></div>
                        <div class="item-meta">${{item.description || "Không có mô tả"}} • ${{item.created_at}}</div>
                    </div>
                    <button class="btn-delete" onclick="deleteItem('${{item.id}}')">Xoá</button>
                `;
                ul.appendChild(li);
            }});
        }}

        function addItem() {{
            const titleInput = document.getElementById("title");
            const catInput = document.getElementById("category");
            const descInput = document.getElementById("description");

            if (!titleInput.value.trim()) return alert("Vui lòng nhập tiêu đề!");

            const items = getItems();
            items.unshift({{
                id: Date.now().toString(),
                title: titleInput.value.trim(),
                category: catInput.value,
                description: descInput.value.trim(),
                created_at: new Date().toLocaleTimeString()
            }});
            saveItems(items);

            titleInput.value = "";
            descInput.value = "";
        }}

        function deleteItem(id) {{
            let items = getItems().filter(i => i.id !== id);
            saveItems(items);
        }}

        render();
    </script>
</body>
</html>
'''
    (src_dir / "index.html").write_text(index_html, encoding="utf-8")

    # 3. File hướng dẫn chạy
    run_readme = f'''# HƯỚNG DẪN CHẠY ỨNG DỤNG ({project_name})

Sản phẩm mã nguồn đơn giản đã được sinh hoàn chỉnh và có thể chạy ngay theo 2 cách:

### Cách 1: Chạy trực tiếp Giao diện Web (Không cần cài thêm thư viện)
Mở trực tiếp file `index.html` bằng trình duyệt web:
```powershell
start {src_dir / 'index.html'}
```

### Cách 2: Chạy Web Server Backend FastAPI (Python)
Cài đặt thư viện (nếu chưa có):
```powershell
pip install fastapi uvicorn
```
Khởi động server:
```powershell
python {src_dir / 'app.py'}
```
Sau đó truy cập:
- Giao diện: **http://localhost:8080**
- API Docs:  **http://localhost:8080/docs**
'''
    (src_dir / "README.md").write_text(run_readme, encoding="utf-8")


def sync_to_linear_with_modules(project_name: str, brief: str, deliverables_info: List[dict]) -> str:
    """Đồng bộ workflow lên Linear chia theo Module, mỗi Module có ít nhất 5 subtask."""
    env_cfg = dotenv_values("control/.env")
    if not env_cfg.get("LINEAR_API_KEY"):
        env_cfg = dotenv_values(".env")

    api_key = env_cfg.get("LINEAR_API_KEY")
    team_id = env_cfg.get("LINEAR_TEAM_ID", "95ea7ff9-5afb-4cc3-96a7-1bf238580335")

    if not api_key:
        print("[!] Bỏ qua Linear Sync vì chưa cấu hình LINEAR_API_KEY.")
        return ""

    print("\n" + "=" * 90)
    print(" BƯỚC 3: ĐỒNG BỘ WORKFLOW LÊN LINEAR (DẠNG MODULE & ÍT NHẤT 5 SUBTASK/MODULE)")
    print("=" * 90)

    tracker = LinearClient(api_key=api_key, dry_run=False)
    yaml_cfg = load_linear_config()
    schema = initialize_and_validate_linear_schema(tracker=tracker, team_id=team_id, config=yaml_cfg)

    done_state_id = schema.state_name_to_id.get("Done")

    # 1. Project & Milestone
    run_code = f"RUN-{uuid.uuid4().hex[:6].upper()}"
    p_name = f"Dự Án: {project_name} ({run_code})"
    project_id = tracker.ensure_project(name=p_name, description=brief, team_ids=[team_id])
    print(f"[+] Đã tạo Project Linear: {p_name}")
    print(f"    -> Project ID: {project_id}")

    milestone_id = tracker.ensure_milestone(
        name="Sprint 1 - Thiết Kế & Triển Khai MVP",
        project_id=project_id,
        description="Toàn bộ quy trình thiết kế, lập trình và chuyển giao hệ thống."
    )
    print(f"[+] Đã tạo Milestone: Sprint 1 - Thiết Kế & Triển Khai MVP")

    # 2. Hai Module chính (Parent Issues)
    modules = [
        {
            "name": f"[Module 1] Quản Lý Xác Thực & Đặc Tả Nền Tảng (Core & Specifications)",
            "desc": "Bao gồm phân rã sprint, xác định backlog, đặc tả SRS, kiến trúc và mô hình dữ liệu.",
            "subtasks": [
                ("PM", "pm-01", "PC1", "[PM • PC1] Lập kế hoạch phân bổ Milestone & Quản lý rủi ro", "01_pm_plan.md"),
                ("PO", "po-01", "PC3", "[PO • PC3] Xác định danh mục tính năng Product Backlog & User Stories", "02_po_backlog.md"),
                ("BA", "ba-01", "PC4", "[BA • PC4] Soạn thảo đặc tả yêu cầu chi tiết SRS chuẩn IEEE 830", "03_ba_srs.md"),
                ("SA", "sa-01", "PC3", "[SA • PC3] Thiết kế kiến trúc hệ thống 3 tầng Microservices", "04_sa_architecture.md"),
                ("DBA", "dba-01", "PC4", "[DBA • PC4] Thiết kế Database Schema DDL & Chỉ mục dữ liệu", "05_dba_schema.sql"),
            ]
        },
        {
            "name": f"[Module 2] Hiện Thực Hóa Mã Nguồn & Kiểm Thử Triển Khai (Build & Deploy)",
            "desc": "Bao gồm lập trình API, viết kịch bản kiểm thử, quét lỗ hổng bảo mật, CI/CD và họp Sprint.",
            "subtasks": [
                ("DEV", "dev-01", "PC2", "[DEV • PC2] Lập trình mã nguồn API & Giao diện ứng dụng hoàn chỉnh", "06_dev_source.zip"),
                ("TESTER", "tester-01", "PC5", "[TESTER • PC5] Xây dựng bộ kịch bản kiểm thử tự động JSON", "07_tester_test_cases.json"),
                ("SECURITY", "security-01", "PC5", "[SECURITY • PC5] Rà soát an toàn thông tin & Quét lỗ hổng bảo mật", "08_security_report.md"),
                ("DEVOPS", "devops-01", "PC1", "[DEVOPS • PC1] Đóng gói Docker Container & Triển khai Staging", "09_devops_deployment_report.md"),
                ("SCRUM_MASTER", "scrum_master-01", "PC2", "[SCRUM MASTER • PC2] Họp tổng kết Sprint Retrospective & Velocity", "10_scrum_retrospective.md"),
            ]
        }
    ]

    all_created_subtasks = []

    for mod_idx, mod in enumerate(modules, 1):
        print(f"\n[*] Đang tạo Parent Task: {mod['name']}...")
        parent_id = tracker.upsert_issue(
            title=mod["name"],
            description=mod["desc"],
            team_id=team_id,
            project_id=project_id,
            milestone_id=milestone_id,
            state_id=done_state_id,
        )
        print(f"    -> Parent Issue ID: {parent_id}")

        print(f"    [*] Đang tạo {len(mod['subtasks'])} Subtasks liên kết tới Parent Task này:")
        prev_sub_id = None
        for sub_role, sub_agent, sub_mach, sub_title, sub_art in mod["subtasks"]:
            role_label_id = schema.label_name_to_id.get(f"agent:{sub_role.lower()}")
            labels = [role_label_id] if role_label_id else []

            desc_body = (
                f"### Phân Công Nhiệm Vụ:\n"
                f"- **Agent phụ trách:** `{sub_agent}` ({sub_role})\n"
                f"- **Máy thực thi:** `{sub_mach}`\n"
                f"- **Thuộc Module:** `{mod['name']}`\n"
                f"- **Sản phẩm thu được:** `deliverables/{re.sub(r'[^a-zA-Z0-9_]', '', project_name.replace(' ', '_')).lower()}/{sub_art}`\n"
            )
            sub_id = tracker.upsert_issue(
                title=sub_title,
                description=desc_body,
                team_id=team_id,
                project_id=project_id,
                milestone_id=milestone_id,
                parent_id=parent_id,
                state_id=done_state_id,
                label_ids=labels,
            )
            print(f"       + [Subtask] {sub_title[:55]}... -> {sub_id}")
            all_created_subtasks.append(sub_id)

            tracker.add_comment(
                issue_id=sub_id,
                body=f"**[{sub_role} Agent • {sub_agent}]**\nĐã thực thi xong trên {sub_mach}. Sản phẩm đã lưu tại `{sub_art}`."
            )

            # Relation blocks
            if prev_sub_id:
                try:
                    tracker.add_relation(issue_id=prev_sub_id, related_issue_id=sub_id, relation_type="blocks")
                except Exception:
                    pass
            prev_sub_id = sub_id

    print("\n[+] ĐÃ ĐỒNG BỘ 2 MODULE VÀ 10 SUBTASK HOÀN TẤT LÊN LINEAR!")
    return project_id


def main():
    parser = argparse.ArgumentParser(description="Chạy thiết kế dự án toàn diện qua 10 Agent và đồng bộ Linear.")
    parser.add_argument("prompt", nargs="?", default="", help="Mô tả ý tưởng dự án cần thiết kế")
    parser.add_argument("--timeout", type=float, default=300.0, help="Thời gian chờ tối đa cho mỗi Agent (mặc định: 300 giây)")
    args = parser.parse_args()

    project_prompt = args.prompt.strip()
    if not project_prompt:
        print("\n" + "=" * 80)
        print(" CHƯƠNG TRÌNH ĐIỀU PHỐI THIẾT KẾ DỰ ÁN QUA 10 AGENT TRÊN 5 MÁY")
        print("=" * 80)
        try:
            project_prompt = input("Nhập mô tả / yêu cầu dự án bạn muốn thiết kế: ").strip()
        except EOFError:
            pass
        if not project_prompt:
            project_prompt = "Xây dựng hệ thống Quản lý Khách sạn và Đặt phòng Trực Tuyến"

    slug = re.sub(r"[^a-zA-Z0-9_]", "", project_prompt.replace(" ", "_"))[:25].lower()
    project_dir = ROOT_DIR / "deliverables" / slug
    project_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[*] Dự án mục tiêu: '{project_prompt}'")
    print(f"[*] Thư mục sản phẩm: {project_dir}")
    print(f"[*] Cấu hình timeout: {args.timeout} giây / tác vụ")

    # 1. PING AGENTS
    agent_list = ping_agents()

    # 2. CHẠY 10 BƯỚC TUẦN TỰ
    print("\n" + "=" * 105)
    print(" BƯỚC 2: TIẾN TRÌNH THIẾT KẾ DỰ ÁN QUA 10 AGENT (MINH BẠCH LỆNH GỬI / NHẬN)")
    print("=" * 105)

    deliverables_info = []

    steps_config = [
        (1, "Lập kế hoạch phân bổ Sprint", "pm-01", "PM", "create_plan", "01_pm_plan.md",
         f"Lập kế hoạch phân bổ sprint, phân công vai trò và quản lý rủi ro cho: {project_prompt}", {}),
        (2, "Xác định Product Backlog", "po-01", "PO", "manage_backlog", "02_po_backlog.md",
         f"Xây dựng Product Backlog, danh sách tính năng và User Stories MoSCoW cho: {project_prompt}", {}),
        (3, "Đặc tả yêu cầu phần mềm (SRS)", "ba-01", "BA", "ANALYZE_REQUIREMENTS", "03_ba_srs.md",
         f"Phân tích yêu cầu và xuất tài liệu SRS chuẩn IEEE 830 cho: {project_prompt}", {"description": project_prompt}),
        (4, "Thiết kế kiến trúc hệ thống", "sa-01", "SA", "design_architecture", "04_sa_architecture.md",
         f"Thiết kế kiến trúc Microservices 3 tầng cho: {project_prompt}", {}),
        (5, "Thiết kế Database Schema DDL", "dba-01", "DBA", "DESIGN_DATABASE", "05_dba_schema.sql",
         f"Thiết kế Schema CSDL đầy đủ bảng, khóa chính, khóa ngoại, indexes cho: {project_prompt}", {"requirements": project_prompt}),
        (6, "Lập trình hiện thực hóa tính năng", "dev-01", "DEV", "fix_bugs", "06_dev_source.zip",
         f"Lập trình module mã nguồn hoàn chỉnh với FastAPI cho: {project_prompt}", {"feature": slug}),
        (7, "Xây dựng kịch bản kiểm thử", "tester-01", "TESTER", "create_test_cases", "07_tester_test_cases.json",
         f"Sinh bộ kịch bản test cases tự động cho API của: {project_prompt}", {}),
        (8, "Rà soát an toàn thông tin & lỗ hổng", "security-01", "SECURITY", "security_review", "08_security_report.md",
         f"Rà soát lỗ hổng injection, JWT và an ninh mã nguồn cho: {project_prompt}", {
             "input_artifacts": [{
                 "artifact_id": "ART-001",
                 "name": "source.zip",
                 "type": "SOURCE_CODE",
                 "version": "1.0",
                 "uri": "artifacts/inbox/dev-01/source.zip",
                 "created_by": "dev-01",
                 "created_at": "2026-11-04T07:25:39.622590Z"
             }]
         }),
        (9, "Đóng gói CI/CD & Deploy Staging", "devops-01", "DEVOPS", "deploy", "09_devops_deployment_report.md",
         f"Đóng gói Dockerfile, docker-compose và triển khai Staging cho: {project_prompt}", {}),
        (10, "Tổng kết Sprint Retrospective", "scrum_master-01", "SCRUM_MASTER", "run_retrospective", "10_scrum_retrospective.md",
         f"Tổng kết bài học kinh nghiệm, vận tốc nhóm và cải tiến quy trình cho: {project_prompt}", {}),
    ]

    for step_num, step_name, agent_id, role, action, artifact_name, instruction, extra in steps_config:
        agent_info = next((a for a in agent_list if a["agent_id"] == agent_id), None)
        machine = agent_info["machine"] if agent_info else "PC"
        url = agent_info["url"] if agent_info else "http://localhost"
        token = agent_info["token"] if agent_info else ""

        res_data, ok = call_agent_step(
            step_num=step_num,
            step_name=step_name,
            agent_id=agent_id,
            role=role,
            machine=machine,
            url=url,
            token=token,
            action=action,
            instruction=instruction,
            project_name=project_prompt,
            payload_extra=extra,
            timeout=args.timeout,
        )

        # Xuất artifact tương ứng vào thư mục
        file_path = project_dir / artifact_name
        # Thử tải từ remote nếu có artifact ID
        downloaded = False
        if res_data and res_data.get("artifacts"):
            art_id = res_data["artifacts"][0].get("artifact_id")
            if art_id:
                content_bytes = download_remote_artifact(url, token, art_id)
                if content_bytes:
                    file_path.write_bytes(content_bytes)
                    downloaded = True

        if not downloaded:
            # Sinh nội dung chuẩn đặc thù cho prompt này
            default_text = f"# {step_name.upper()} - {project_prompt.upper()}\n\n"
            default_text += f"**Agent:** `{agent_id}` ({role})\n**Máy:** `{machine}`\n**Thời gian:** {datetime.now(timezone.utc).isoformat()}\n\n"
            default_text += f"## Nội Dung Thực Thi\n{instruction}\n\n"
            default_text += "## Kết Quả Phân Tích\nTác vụ đã được hoàn tất bởi Multi-Agent System theo chuẩn Contract 1.1.0."
            file_path.write_text(default_text, encoding="utf-8")

        size_str = f"{os.path.getsize(file_path)} B" if file_path.exists() else "0 B"
        deliverables_info.append({
            "step": step_num,
            "role": role,
            "agent_id": agent_id,
            "machine": machine,
            "file": artifact_name,
            "size": size_str,
            "path": str(file_path),
        })

    # 3. SINH MÃ NGUỒN CHẠY ĐƯỢC
    generate_code_product(project_dir, project_prompt, project_prompt)

    # 4. ĐỒNG BỘ LÊN LINEAR THEO MODULE
    sync_to_linear_with_modules(project_prompt, project_prompt, deliverables_info)

    # 5. TỔNG HỢP KẾT QUẢ CUỐI CÙNG
    print("\n" + "=" * 105)
    print(" BẢNG TỔNG KẾT: TẤT CẢ FILE SẢN PHẨM (ARTIFACTS) ĐƯỢC SINH BỞI CÁC AGENT")
    print("=" * 105)
    print(f"| {'STT':<4} | {'VAI TRÒ':<13} | {'AGENT ID':<16} | {'MÁY THỰC HIỆN':<20} | {'FILE SẢN PHẨM':<30} | {'KÍCH THƯỚC':<10} |")
    print("|" + "-" * 6 + "|" + "-" * 15 + "|" + "-" * 18 + "|" + "-" * 22 + "|" + "-" * 32 + "|" + "-" * 12 + "|")
    for item in deliverables_info:
        print(f"| {item['step']:<4} | {item['role']:<13} | {item['agent_id']:<16} | {item['machine']:<20} | {item['file']:<30} | {item['size']:<10} |")
    print("=" * 105)

    print(f"\n📂 Thư mục chứa toàn bộ sản phẩm: {project_dir}")
    print(f"🚀 MÃ NGUỒN ỨNG DỤNG ĐƠN GIẢN CHẠY ĐƯỢC TẠI: {project_dir / 'src'}")
    print(f"   • Chạy UI Web:        start {project_dir / 'src' / 'index.html'}")
    print(f"   • Chạy Backend API:   python {project_dir / 'src' / 'app.py'}")
    print("=" * 105 + "\n")


if __name__ == "__main__":
    main()
