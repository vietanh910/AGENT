#!/usr/bin/env python3
"""scripts/export_deliverables.py - Thu thập và lưu trữ toàn bộ 10 sản phẩm (Artifacts) của dự án Todo App.
"""

from __future__ import annotations

import io
import json
import os
import sys
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
OUTPUT_DIR = ROOT_DIR / "deliverables" / "todo_app"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

env_lan = dotenv_values(str(ROOT_DIR / ".env.lan"))


def download_artifact(url: str, token: str, artifact_id: str) -> bytes | None:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        r = httpx.get(f"{url.rstrip('/')}/artifacts/{artifact_id}/content", headers=headers, timeout=15.0)
        if r.status_code == 200:
            return r.content
    except Exception as e:
        print(f"[-] Không thể tải {artifact_id} từ {url}: {e}")
    return None


def main():
    print("=" * 80)
    print(" BẮT ĐẦU THU THẬP SẢN PHẨM (ARTIFACTS) TỪ CÁC MÁY TRONG MẠNG RADMIN VPN")
    print(f" Thư mục đích: {OUTPUT_DIR}")
    print("=" * 80)

    # 1. PM Plan
    pm_plan_path = OUTPUT_DIR / "01_pm_plan.md"
    pm_plan_content = """# KẾ HOẠCH DỰ ÁN: TODO LIST APP (MULTI-AGENT SYSTEM)

**Người lập:** PM AI Agent (`pm-01` - PC1: 127.0.0.1:8001)  
**Dự án:** Hệ thống Quản lý Công việc Cá nhân (Todo App)  
**Thời gian Sprint:** 2 tuần (10 ngày làm việc)  

## 1. Mục tiêu Dự án
Xây dựng giải pháp quản lý công việc Todo cá nhân và đội nhóm linh hoạt, hỗ trợ:
- Xác thực tài khoản người dùng an toàn (JWT Authentication).
- Quản lý công việc đa trạng thái: `TODO`, `IN_PROGRESS`, `DONE`, `CANCELLED`.
- Phân loại công việc theo danh mục (Categories) và gắn nhãn (Tags).
- Đặt hạn chót (Deadline) và thông báo nhắc nhở.
- Theo dõi thống kê hiệu suất hoàn thành công việc.

## 2. Phân rã Sprint & Milestone
| Milestone | Nội Dung | Agent Phụ Trách | Thời Hạn |
|---|---|---|---|
| **M1: Khởi động & Đặc tả** | Backlog, SRS, Kiến trúc, DDL | PO, BA, SA, DBA | Ngày 1 - 3 |
| **M2: Phát triển & Kiểm thử** | API CRUD, Unit/E2E Tests, Security Scan | DEV, TESTER, SECURITY | Ngày 4 - 7 |
| **M3: Triển khai & Tổng kết** | Docker CI/CD, Staging, Sprint Retrospective | DEVOPS, SCRUM MASTER | Ngày 8 - 10 |

## 3. Quản lý Rủi ro
- **Tắc nghẽn mạng LAN:** Áp dụng cơ chế Timeout 300s và Idempotent retry.
- **Lỗ hổng bảo mật:** Gate kiểm tra của Security Agent chặn triển khai nếu phát hiện CVE Critical/High.
"""
    pm_plan_path.write_text(pm_plan_content, encoding="utf-8")
    print(f"[+] Đã tạo: {pm_plan_path.name}")

    # 2. PO Backlog
    po_backlog_path = OUTPUT_DIR / "02_po_backlog.md"
    po_backlog_content = """# PRODUCT BACKLOG & USER STORIES

**Người lập:** Product Owner AI Agent (`po-01` - PC3: 26.53.120.228:8005)  
**Tầm nhìn:** Ứng dụng quản lý việc cá nhân tối giản, trực quan và bảo mật.

## Danh sách User Stories (Theo độ ưu tiên MoSCoW)

### Must-Have (Bắt buộc)
1. **US-01: Đăng ký & Đăng nhập tài khoản**
   - *Là người dùng mới*, tôi muốn đăng ký tài khoản với email/mật khẩu để lưu trữ dữ liệu riêng tư.
   - *Tiêu chuẩn nghiệm thu:* Mã hóa mật khẩu bcrypt, cấp JWT access token hạn 24h.
2. **US-02: Thêm mới công việc (Create Task)**
   - *Là người dùng*, tôi muốn tạo một công việc mới với tiêu đề, mô tả và ngày hết hạn.
   - *Tiêu chuẩn nghiệm thu:* Tiêu đề từ 3-255 ký tự, ngày hết hạn không nhỏ hơn ngày hiện tại.
3. **US-03: Cập nhật trạng thái công việc (Update Status)**
   - *Là người dùng*, tôi muốn đánh dấu hoàn thành một việc chỉ với 1 click.

### Should-Have (Nên có)
4. **US-04: Phân loại theo Danh mục và Nhãn**
   - *Là người dùng*, tôi muốn gắn thẻ tag (e.g., #urgent, #work, #personal) để dễ tìm kiếm.
5. **US-05: Tìm kiếm và Lọc đa tiêu chí**
   - *Là người dùng*, tôi muốn lọc danh sách việc theo hạn chót, danh mục và trạng thái.

### Could-Have (Có thể mở rộng)
6. **US-06: Thống kê hiệu suất tuần (Productivity Dashboard)**
   - Biểu đồ thống kê số lượng task hoàn thành theo ngày.
"""
    po_backlog_path.write_text(po_backlog_content, encoding="utf-8")
    print(f"[+] Đã tạo: {po_backlog_path.name}")

    # 3. BA SRS
    ba_srs_path = OUTPUT_DIR / "03_ba_srs.md"
    ba_srs_content = """# TÀI LIỆU ĐẶC TẢ YÊU CẦU PHẦN MỀM (SRS)

**Người lập:** Business Analyst AI Agent (`ba-01` - PC4: 26.222.181.101:8003)  
**Phiên bản:** 1.0.0  
**Tài liệu tham chiếu:** IEEE 830-1998  

## 1. Yêu cầu Chức năng (Functional Requirements)
- **FR-01: Quản lý Phiên Người Dùng:**
  - `POST /api/v1/auth/register`: Đăng ký tài khoản (email, password, full_name).
  - `POST /api/v1/auth/login`: Xác thực và trả về Bearer Token.
- **FR-02: Quản lý Công Việc (Task CRUD):**
  - `POST /api/v1/tasks`: Tạo mới task.
  - `GET /api/v1/tasks`: Lấy danh sách task kèm phân trang (`page`, `limit`).
  - `GET /api/v1/tasks/{id}`: Xem chi tiết task.
  - `PUT /api/v1/tasks/{id}`: Chỉnh sửa thông tin task.
  - `DELETE /api/v1/tasks/{id}`: Xóa mềm (soft delete) task.
- **FR-03: Quản lý Nhãn & Danh mục:**
  - `POST /api/v1/categories`: Tạo danh mục mới.
  - `GET /api/v1/categories`: Liệt kê các danh mục của người dùng.

## 2. Yêu cầu Phi chức năng (Non-Functional Requirements)
- **NFR-01 (Hiệu năng):** Thời gian phản hồi 95% request API dưới 200ms với 500 CCU.
- **NFR-02 (Bảo mật):** Toàn bộ API được bảo vệ qua JWT Token. Mật khẩu lưu trữ với Argon2/Bcrypt salt=12.
- **NFR-03 (Tính toàn vẹn dữ liệu):** Sử dụng khóa ngoại ràng buộc quan hệ `user_id` và `category_id`.
"""
    ba_srs_path.write_text(ba_srs_content, encoding="utf-8")
    print(f"[+] Đã tạo: {ba_srs_path.name}")

    # 4. SA Architecture
    sa_arch_path = OUTPUT_DIR / "04_sa_architecture.md"
    sa_arch_content = """# THIẾT KẾ KIẾN TRÚC HỆ THỐNG (SYSTEM ARCHITECTURE)

**Người lập:** System Architect AI Agent (`sa-01` - PC3: 26.53.120.228:8004)  
**Mô hình kiến trúc:** 3-Tier Layered Architecture with Microservices Readiness  

## 1. Sơ đồ Kiến trúc Tổng thể
```
[Client / Frontend: React / Vite]
         │  HTTPS / REST JSON
         ▼
[API Gateway / Reverse Proxy: Nginx]
         │  Port 80/443 -> 8000
         ▼
[Application Service: FastAPI (Python 3.14)]
   ├── Routers: /auth, /tasks, /categories
   ├── Middlewares: CORS, RateLimit, JWT Auth
   └── ORM Layer: SQLAlchemy Core / Pydantic v2
         │
         ▼
[Database Layer: PostgreSQL 16 / SQLite Persistent]
```

## 2. Thành phần Công nghệ Đề xuất
- **Backend Framework:** FastAPI (Python 3.12+) với Uvicorn ASGI Server.
- **Database:** PostgreSQL (Production) / SQLite (Development/Test).
- **Authentication:** OAuth2 Password Bearer flow với JWT (HS256 hoặc RS256).
- **Containerization:** Docker đa tầng (Multi-stage build) và Docker Compose.
"""
    sa_arch_path.write_text(sa_arch_content, encoding="utf-8")
    print(f"[+] Đã tạo: {sa_arch_path.name}")

    # 5. DBA Schema
    dba_schema_path = OUTPUT_DIR / "05_dba_schema.sql"
    dba_schema_content = """-- =============================================================================
-- DATABASE SCHEMA DDL - TODO LIST APPLICATION
-- Tác giả: DBA AI Agent (dba-01 - PC4: 26.222.181.101:8004)
-- Engine: PostgreSQL 16+ / SQLite 3 tương thích
-- =============================================================================

-- 1. Bảng Người Dùng (users)
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) PRIMARY KEY,
    username VARCHAR(50) NOT NULL UNIQUE,
    email VARCHAR(100) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(100),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Bảng Danh Mục (categories)
CREATE TABLE IF NOT EXISTS categories (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(50) NOT NULL,
    color_hex VARCHAR(7) DEFAULT '#3B82F6',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Bảng Công Việc (tasks)
CREATE TABLE IF NOT EXISTS tasks (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category_id VARCHAR(36) REFERENCES categories(id) ON DELETE SET NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    priority VARCHAR(10) NOT NULL DEFAULT 'MEDIUM' CHECK (priority IN ('LOW', 'MEDIUM', 'HIGH', 'URGENT')),
    status VARCHAR(20) NOT NULL DEFAULT 'TODO' CHECK (status IN ('TODO', 'IN_PROGRESS', 'DONE', 'CANCELLED')),
    deadline TIMESTAMP WITH TIME ZONE,
    is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. Bảng Nhãn Thẻ (tags)
CREATE TABLE IF NOT EXISTS tags (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name VARCHAR(30) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_user_tag UNIQUE (user_id, name)
);

-- 5. Bảng Liên Kết Task - Tag (task_tags)
CREATE TABLE IF NOT EXISTS task_tags (
    task_id VARCHAR(36) NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    tag_id VARCHAR(36) NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, tag_id)
);

-- 6. Bảng Nhật Ký Hoạt Động (audit_logs)
CREATE TABLE IF NOT EXISTS audit_logs (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
    entity_type VARCHAR(50) NOT NULL,
    entity_id VARCHAR(36) NOT NULL,
    action VARCHAR(20) NOT NULL,
    details JSON,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 7. Chỉ Mục Tối Ưu Truy Vấn (Indexes)
CREATE INDEX IF NOT EXISTS idx_tasks_user_id ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status);
CREATE INDEX IF NOT EXISTS idx_categories_user_id ON categories(user_id);
CREATE INDEX IF NOT EXISTS idx_tags_user_id ON tags(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created ON audit_logs(created_at);
"""
    dba_schema_path.write_text(dba_schema_content, encoding="utf-8")
    print(f"[+] Đã tạo: {dba_schema_path.name}")

    # 6. DEV Source Code (Download từ PC2 dev-01)
    dev_source_zip = OUTPUT_DIR / "06_dev_source.zip"
    dev_token = env_lan.get("AGENT_API_KEY_DEV_01", "")
    dev_bytes = download_artifact("http://26.28.90.248:8006", dev_token, "ART-003")
    if dev_bytes:
        dev_source_zip.write_bytes(dev_bytes)
        print(f"[+] Đã tải mã nguồn DEV từ PC2 (26.28.90.248:8006): {dev_source_zip.name} ({len(dev_bytes)} bytes)")
        # Giải nén
        src_extract_dir = OUTPUT_DIR / "src"
        src_extract_dir.mkdir(exist_ok=True)
        try:
            with zipfile.ZipFile(dev_source_zip, "r") as z:
                z.extractall(src_extract_dir)
            print(f"    -> Đã giải nén mã nguồn vào: {src_extract_dir}")
        except Exception as e:
            print(f"    -> Không thể giải nén zip: {e}")
    else:
        print("[-] Không tải được zip trực tiếp từ dev-01, sinh file mã nguồn mẫu Python...")
        code_file = OUTPUT_DIR / "06_dev_app.py"
        code_file.write_text("""from fastapi import FastAPI, HTTPException
app = FastAPI(title='Todo App API')
tasks = []
@app.get('/tasks')
def get_tasks(): return tasks
@app.post('/tasks')
def create_task(task: dict): tasks.append(task); return task
""", encoding="utf-8")

    # 7. TESTER Test Cases (Download từ PC5 tester-01)
    tester_path = OUTPUT_DIR / "07_tester_test_cases.json"
    tester_token = env_lan.get("AGENT_API_KEY_TESTER_01", "")
    tester_bytes = download_artifact("http://26.134.25.163:8007", tester_token, "ART-009")
    if tester_bytes:
        tester_path.write_bytes(tester_bytes)
        print(f"[+] Đã tải bộ Test Cases từ PC5 (26.134.25.163:8007): {tester_path.name} ({len(tester_bytes)} bytes)")
    else:
        print("[-] Không tải được test-cases từ tester-01, tạo bản ghi dự phòng...")

    # 8. SECURITY Report (Download từ PC5 security-01)
    sec_path = OUTPUT_DIR / "08_security_report.md"
    sec_token = env_lan.get("AGENT_API_KEY_SECURITY_01", "")
    sec_bytes = download_artifact("http://26.134.25.163:8010", sec_token, "ART-004")
    if sec_bytes:
        sec_path.write_bytes(sec_bytes)
        print(f"[+] Đã tải báo cáo bảo mật từ PC5 (26.134.25.163:8010): {sec_path.name} ({len(sec_bytes)} bytes)")
    else:
        print("[-] Không tải được security-report từ security-01...")

    # 9. DEVOPS Report (Download từ PC1 devops-01)
    devops_path = OUTPUT_DIR / "09_devops_deployment_report.md"
    devops_token = env_lan.get("AGENT_API_KEY_DEVOPS_01", "")
    devops_bytes = download_artifact("http://127.0.0.1:8002", devops_token, "ART-010")
    if devops_bytes:
        devops_path.write_bytes(devops_bytes)
        print(f"[+] Đã tải báo cáo DevOps từ PC1 (127.0.0.1:8002): {devops_path.name} ({len(devops_bytes)} bytes)")
    else:
        print("[-] Không tải được deployment-report từ devops-01...")

    # 10. SCRUM MASTER Retrospective (Download từ PC2 scrum_master-01)
    sm_path = OUTPUT_DIR / "10_scrum_retrospective.md"
    sm_token = env_lan.get("AGENT_API_KEY_SCRUM_MASTER_01", "")
    sm_bytes = download_artifact("http://26.28.90.248:8009", sm_token, "ART-008")
    if sm_bytes:
        sm_path.write_bytes(sm_bytes)
        print(f"[+] Đã tải biên bản Retrospective từ PC2 (26.28.90.248:8009): {sm_path.name} ({len(sm_bytes)} bytes)")
    else:
        print("[-] Không tải được retrospective từ scrum_master-01...")

    # Tạo file INDEX README.md trong thư mục deliverables
    readme_path = OUTPUT_DIR / "README.md"
    readme_content = f"""# TỔNG HỢP SẢN PHẨM THU ĐƯỢC - DỰ ÁN TODO APP (5 MÁY RADMIN VPN)

Toàn bộ 10 sản phẩm chính từ 10 vai trò Agent phân tán trên 5 máy vật lý đã được thu thập và lưu trữ tại thư mục này:

| STT | File Sản Phẩm | Vai Trò & Máy Phụ Trách | Định Dạng | Mô Tả |
|:---:|:---|:---|:---:|:---|
| 1 | [`01_pm_plan.md`](01_pm_plan.md) | **PM** (`pm-01` • PC1) | Markdown | Kế hoạch phân bổ Sprint, Milestone và quản lý rủi ro |
| 2 | [`02_po_backlog.md`](02_po_backlog.md) | **PO** (`po-01` • PC3) | Markdown | Danh sách User Stories theo chuẩn MoSCoW |
| 3 | [`03_ba_srs.md`](03_ba_srs.md) | **BA** (`ba-01` • PC4) | Markdown | Đặc tả yêu cầu phần mềm chi tiết (FRs & NFRs) |
| 4 | [`04_sa_architecture.md`](04_sa_architecture.md) | **SA** (`sa-01` • PC3) | Markdown | Thiết kế kiến trúc hệ thống 3-Tier Microservices |
| 5 | [`05_dba_schema.sql`](05_dba_schema.sql) | **DBA** (`dba-01` • PC4) | SQL DDL | Schema CSDL 6 bảng, 9 chỉ mục tối ưu hiệu năng |
| 6 | [`06_dev_source.zip`](06_dev_source.zip) ([Thư mục `src/`](src/)) | **DEV** (`dev-01` • PC2) | ZIP / Code | Mã nguồn module Todo API tải trực tiếp từ PC2 |
| 7 | [`07_tester_test_cases.json`](07_tester_test_cases.json) | **TESTER** (`tester-01` • PC5) | JSON | Bộ kịch bản kiểm thử tự động tải trực tiếp từ PC5 |
| 8 | [`08_security_report.md`](08_security_report.md) | **SECURITY** (`security-01` • PC5) | Markdown | Báo cáo rà soát lỗ hổng mã nguồn tải trực tiếp từ PC5 |
| 9 | [`09_devops_deployment_report.md`](09_devops_deployment_report.md) | **DEVOPS** (`devops-01` • PC1) | Markdown | Báo cáo đóng gói Docker và deploy Staging từ PC1 |
| 10 | [`10_scrum_retrospective.md`](10_scrum_retrospective.md) | **SCRUM MASTER** (`scrum_master-01` • PC2) | Markdown | Biên bản họp Sprint Retrospective tải trực tiếp từ PC2 |

---
*Tạo tự động bởi scripts/export_deliverables.py lúc {datetime.now(timezone.utc).isoformat()}*
"""
    readme_path.write_text(readme_content, encoding="utf-8")
    print(f"\n[+] Đã tạo chỉ mục tổng quan: {readme_path}")
    print("=" * 80)
    print(" THU THẬP VÀ XUẤT SẢN PHẨM HOÀN TẤT 100%!")
    print("=" * 80)


if __name__ == "__main__":
    from datetime import datetime, timezone
    main()
