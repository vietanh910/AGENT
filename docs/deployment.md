# Hướng dẫn triển khai và Phân quyền GitHub Token (docs/deployment.md)

Tài liệu hướng dẫn triển khai hệ thống phân tán đa máy (PC1 đến PC5) và chính sách phân quyền GitHub Access Token cho các thành phần.

---

## 1. Phân quyền GitHub Token (Least Privilege Policy)

Để đảm bảo an toàn tuyệt đối cho repository mã nguồn chính (`vietanh910/AGENT`), toàn bộ quyền hạn tương tác với GitHub được phân tách nghiêm ngặt theo nguyên tắc đặc quyền tối thiểu:

### A. Worker Host PC4 (DEV: dev-01 & TESTER: tester-01)
- **Loại Token**: GitHub Fine-Grained Personal Access Token (PAT).
- **Phạm vi (Resource Scope)**: Chỉ áp dụng DUY NHẤT cho repository dự án cụ thể (`vietanh910/AGENT`).
- **Quyền hạn (Repository Permissions)**:
  - `Contents`: **Read and Write** (để clone repo, tạo branch `agent/<task_id>`, push code lên branch của task).
  - `Pull requests`: **Read and Write** (để mở Pull Request tự động từ branch của task vào `main`).
  - `Metadata`: **Read-only** (mặc định bắt buộc của GitHub).
- **Quyền bị CẤM TUYỆT ĐỐI trên PC4**:
  - **KHÔNG** cấp quyền `Administration`.
  - **KHÔNG** cấp quyền bypass branch protection rules.
  - **KHÔNG** cấp quyền Merge Pull Request vào branch `main` hoặc `master`.
  - Mã nguồn trên PC4 bị chặn force push (`--force`, `-f`) và chặn push thẳng vào `main` tại tầng `TerminalExecutor` và `GitWorkspace`.

### B. Control Node PC1 (DevOps Adapter & Orchestrator)
- **Loại Token**: GitHub Token dành riêng cho DevOps / Automation CI.
- **Quyền hạn**:
  - `Contents`: Read and Write.
  - `Pull requests`: **Write** kèm quyền **Merge Pull Request**.
  - `Checks` / `Commit statuses`: Read (để kiểm tra kết quả CI/CD Check Runs).
- **Nguyên tắc kích hoạt**:
  - Chỉ Orchestrator trên PC1 thông qua adapter `control/app/integrations/github/pull_requests.py` mới được quyền gọi `merge_pull_request`.
  - Hàm merge bắt buộc kiểm tra tham số `policy_ok=True` (chỉ bật khi tất cả các kiểm thử của TESTER, rà soát của SECURITY, và các cổng Gate của PO/PM đều đạt trạng thái `PASS`).
  - Mọi thao tác merge đều tự động ghi Audit Log bền vững vào bảng `audit_logs` trên MySQL.

---

## 2. Bảng phân bố cấu hình mạng và Token

| Máy | Vai trò | Agents | Token GitHub | Quyền hạn GitHub | Quyền Merge vào `main` |
| --- | --- | --- | --- | --- | --- |
| **PC1** | Control Node | `pm-01`, `devops-01` | Token DevOps | Contents (RW), PR (RW), Checks (R) | **CÓ** (qua Policy Guard `policy_ok=True`) |
| **PC2** | Worker Host | `sa-01`, `dba-01` | *Không cần* | - | KHÔNG |
| **PC3** | Worker Host | `ba-01`, `po-01` | *Không cần* | - | KHÔNG |
| **PC4** | Worker Host | `dev-01`, `tester-01` | Token Fine-Grained PC4 | Contents (RW), PR (RW) | **KHÔNG** (Bị cấm hoàn toàn) |
| **PC5** | Worker Host | `scrum_master-01`, `security-01` | Token Read-only (nếu cần) | Contents (R), PR (R) | KHÔNG |

---

## 3. Hướng dẫn Triển khai trên 5 Máy Vật lý (Physical Deployment)

### Bước 1: Yêu cầu Môi trường chung
- Python 3.11+ (khuyến nghị Python 3.12 hoặc 3.14).
- Git 2.40+.
- Kết nối LAN ổn định (ping giữa các máy < 5ms).

### Bước 2: Triển khai PC1 Control Node (192.168.1.101)
1. Clone repository và thiết lập môi trường:
   ```bash
   git clone https://github.com/vietanh910/AGENT.git
   cd AGENT
   python -m venv venv
   source venv/bin/activate  # Hoặc venv\Scripts\activate trên Windows
   pip install -r requirements.txt
   pip install -e contract/
   ```
2. Khởi tạo file cấu hình `.env` từ `control/.env.example`:
   ```bash
   cp control/.env.example control/.env
   ```
3. Cấu hình các biến môi trường thiết yếu:
   - `DATABASE_URL`: `mysql+pymysql://ai_user:ai_password@127.0.0.1:3307/ai_team?charset=utf8mb4`
   - `REDIS_URL`: `redis://127.0.0.1:6379/0`
   - `ADMIN_TOKEN`: Khóa bí mật dành riêng cho quản trị viên PC1
   - `LINEAR_API_KEY`: API key của Linear team
4. Khởi động hạ tầng và dịch vụ Control Node:
   ```bash
   python scripts/start_control.py
   ```

### Bước 3: Triển khai Worker Hosts (PC2 đến PC5)
Trên từng máy Worker tương ứng:
1. Clone repo và cài đặt dependency:
   ```bash
   git clone https://github.com/vietanh910/AGENT.git
   cd AGENT
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   pip install -e contract/
   ```
2. Sao chép file cấu hình tương ứng máy:
   - **PC2**: `cp worker/worker.pc2.yaml.example worker/worker.yaml`
   - **PC3**: `cp worker/worker.pc3.yaml.example worker/worker.yaml`
   - **PC4**: `cp worker/worker.pc4.yaml.example worker/worker.yaml`
   - **PC5**: `cp worker/worker.pc5.yaml.example worker/worker.yaml`
3. Kiểm tra địa chỉ `control_url` trỏ về IP của PC1 (`http://192.168.1.101:8000`).
4. Khởi động Worker Host:
   ```bash
   python scripts/start_worker.py --config worker/worker.yaml
   ```

---

## 4. Quản lý Tiến trình Dịch vụ (Service Management)

Để đảm bảo các tiến trình tự động khởi động khi máy bật lại:
- **Trên Windows**: Sử dụng công cụ NSSM (Non-Sucking Service Manager) hoặc Windows Task Scheduler để tạo Windows Service cho `scripts/start_control.py` (PC1) và `scripts/start_worker.py` (PC2..PC5).
- **Trên Linux**: Tạo systemd unit file tại `/etc/systemd/system/ai-worker.service`:
  ```ini
  [Unit]
  Description=AI Team Worker Host
  After=network.target

  [Service]
  Type=simple
  User=ubuntu
  WorkingDirectory=/home/ubuntu/AGENT
  ExecStart=/home/ubuntu/AGENT/venv/bin/python scripts/start_worker.py --config worker/worker.yaml
  Restart=always
  RestartSec=5

  [Install]
  WantedBy=multi-user.target
  ```

