# Hướng dẫn triển khai và Phân quyền GitHub Token (docs/deployment.md)

Tài liệu hướng dẫn triển khai hệ thống phân tán đa máy (PC1 đến PC5 qua Radmin VPN) và chính sách phân quyền GitHub Access Token cho các thành phần.

---

## 1. Phân quyền GitHub Token (Least Privilege Policy)

Để đảm bảo an toàn tuyệt đối cho repository mã nguồn chính (`vietanh910/AGENT`), toàn bộ quyền hạn tương tác với GitHub được phân tách nghiêm ngặt theo nguyên tắc đặc quyền tối thiểu:

### A. Worker Host PC2 (DEV: dev-01 & SCRUM_MASTER: scrum_master-01)
- **Loại Token**: GitHub Fine-Grained Personal Access Token (PAT).
- **Phạm vi (Resource Scope)**: Chỉ áp dụng DUY NHẤT cho repository dự án cụ thể (`vietanh910/AGENT`).
- **Quyền hạn (Repository Permissions)**:
  - `Contents`: **Read and Write** (để clone repo, tạo branch `agent/<task_id>`, push code lên branch của task).
  - `Pull requests`: **Read and Write** (để mở Pull Request tự động từ branch của task vào `main`).
  - `Metadata`: **Read-only** (mặc định bắt buộc của GitHub).
- **Quyền bị CẤM TUYỆT ĐỐI trên PC2**:
  - **KHÔNG** cấp quyền `Administration`.
  - **KHÔNG** cấp quyền bypass branch protection rules.
  - **KHÔNG** cấp quyền Merge Pull Request vào branch `main` hoặc `master`.
  - Mã nguồn trên PC2 bị chặn force push (`--force`, `-f`) và chặn push thẳng vào `main` tại tầng `TerminalExecutor` và `GitWorkspace`.

### B. Control Node PC1 (DevOps Adapter & Orchestrator)
- **Loại Token**: GitHub Token dành riêng cho DevOps / Automation CI (`<GITHUB_TOKEN>`).
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

| Máy | Định danh | IP Radmin VPN | Agents | Token GitHub | Quyền hạn GitHub | Quyền Merge vào `main` |
| --- | --- | --- | --- | --- | --- | --- |
| **PC1** | `PC1` | `26.105.63.241` | `pm-01`, `devops-01` | Token DevOps | Contents (RW), PR (RW), Checks (R) | **CÓ** (qua Policy Guard `policy_ok=True`) |
| **PC2** | `PC2` | `26.28.90.248` | `dev-01`, `scrum_master-01` | Token Fine-Grained PC2 | Contents (RW), PR (RW) | **KHÔNG** (Bị cấm hoàn toàn) |
| **PC3** | `PC3` | `26.53.120.228` | `po-01`, `sa-01` | *Không cần* | - | KHÔNG |
| **PC4** | `PC4` | `26.222.181.101` | `ba-01`, `dba-01` | *Không cần* | - | KHÔNG |
| **PC5** | `PC5` | `26.134.25.163` | `tester-01`, `security-01` | Token Read-only (nếu cần) | Contents (R), PR (R) | KHÔNG |

---

## 3. Hướng dẫn Triển khai trên 5 Máy Vật lý (Physical Deployment)

### Bước 1: Yêu cầu Môi trường chung
- Cả 5 máy cùng tham gia một mạng Radmin VPN (`26.0.0.0/8`).
- Python 3.11+ (Python 3.12 hoặc 3.14).
- Git 2.40+.

### Bước 2: Triển khai PC1 Control Node (26.105.63.241)
1. Kiểm tra Git branch và pull mã nguồn mới nhất:
   ```bash
   git status
   git pull origin main
   ```
2. Khởi động hạ tầng Docker (MySQL, Redis):
   ```bash
   docker compose -f control/docker-compose.yml up -d
   ```
3. Mở cổng firewall 8000:
   ```powershell
   netsh advfirewall firewall add rule name="AI-Control-Port" dir=in action=allow protocol=TCP localport=8000
   ```
4. Khởi động Control Node:
   ```bash
   python scripts/start_control.py --port 8000
   ```

### Bước 3: Triển khai Worker Hosts (PC2 đến PC5)
Trên từng máy Worker tương ứng:
1. Clone repo hoặc git pull:
   ```bash
   git clone https://github.com/vietanh910/AGENT.git
   cd AGENT
   git checkout main
   git pull origin main
   ```
2. Mở cổng firewall 8001:
   ```powershell
   netsh advfirewall firewall add rule name="AI-Worker-Port" dir=in action=allow protocol=TCP localport=8001
   ```
3. Cài đặt các gói phụ thuộc:
   ```bash
   pip install -r requirements.txt
   pip install -e contract/
   ```
4. Khởi động Worker Host:
   - **PC2** (26.28.90.248):
     ```bash
     python scripts/start_worker.py --config worker/worker.pc2.yaml --port 8001
     ```
   - **PC3** (26.53.120.228):
     ```bash
     python scripts/start_worker.py --config worker/worker.pc3.yaml --port 8001
     ```
   - **PC4** (26.222.181.101):
     ```bash
     python scripts/start_worker.py --config worker/worker.pc4.yaml --port 8001
     ```
   - **PC5** (26.134.25.163):
     ```bash
     python scripts/start_worker.py --config worker/worker.pc5.yaml --port 8001
     ```

### Bước 4: Kiểm tra Kết nối từ PC1
Từ PC1 (Control Node), chạy lệnh:
```bash
python scripts/lan_check.py
```
Khi cả 4 Worker hiển thị **PASS**, hệ thống đã sẵn sàng thực hiện WorkflowRun!
