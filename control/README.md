# Hướng dẫn Cài đặt & Vận hành Control Node (PC1)

Control Node đóng vai trò là trung tâm điều phối của toàn bộ hệ sinh thái **ai-team**:
- Chạy **Gateway API** (FastAPI) tiếp nhận yêu cầu và callback từ 4 Worker Host.
- Chạy **Orchestrator** (LangGraph với trạng thái bền vững trong MySQL 8.4 LTS).
- Quản lý **Hạ tầng dữ liệu**: MySQL 8.4 LTS và Redis 7 qua Docker Compose.
- Chạy trực tiếp 2 Agent cục bộ: **PM Agent** và **DevOps Agent** (qua `LocalWorker`).
- Kết nối phản chiếu công việc sang **Linear** và quản lý mã nguồn qua **GitHub**.

---

## 1. Yêu cầu Tiên quyết
- **Python**: Phiên bản 3.11 trở lên (khuyến nghị Python 3.11 hoặc 3.12).
- **Git**: Đã cài đặt và có trong `PATH`.
- **Docker & Docker Compose**: Docker Compose v2 trở lên để quản trị MySQL 8.4 và Redis.
- **LAN**: Địa chỉ IP cố định (Static IP) hoặc DHCP reservation trong mạng nội bộ (ví dụ: `192.168.1.10`).

---

## 2. Hướng dẫn Cài đặt theo Từng Hệ điều hành

### A. Windows (Native & WSL2)
1. **Cài đặt Docker Desktop**:
   - Tải và cài đặt Docker Desktop từ [docker.com](https://www.docker.com/products/docker-desktop/).
   - Trong Docker Desktop Settings > General, bật **Use the WSL 2 based engine**.
2. **Cài đặt Python 3.11+**:
   - Tải bộ cài từ [python.org](https://www.python.org/downloads/) (tích chọn *Add python.exe to PATH*).
3. **Thiết lập Môi trường ảo (Virtualenv)**:
   ```powershell
   python -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
4. **Cấu hình Firewall trên Windows**:
   - Mở cổng 8000 TCP cho mạng Private/LAN để các máy PC2..PC5 có thể gọi tới Gateway:
   ```powershell
   New-NetFirewallRule -DisplayName "Control Node Gateway 8000" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow
   ```

### B. macOS
1. **Cài đặt công cụ qua Homebrew**:
   ```bash
   brew install python@3.11 git
   brew install --cask docker
   ```
2. **Khởi động Docker Desktop** và kiểm tra `docker compose version`.
3. **Thiết lập Môi trường ảo**:
   ```bash
   python3.11 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

### C. Linux (Ubuntu 22.04 / 24.04 LTS)
1. **Cài đặt Docker Engine & Docker Compose**:
   ```bash
   sudo apt-get update
   sudo apt-get install -y ca-certificates curl gnupg
   sudo install -m 0755 -d /etc/apt/keyrings
   curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
   sudo chmod a+r /etc/apt/keyrings/docker.gpg

   echo \
     "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
     $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
     sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

   sudo apt-get update
   sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin python3.11 python3.11-venv
   sudo usermod -aG docker $USER
   ```
2. **Thiết lập Môi trường ảo**:
   ```bash
   python3.11 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

---

## 3. Cấu hình Môi trường (.env)

Sao chép tệp mẫu cấu hình sang `.env` ở thư mục gốc hoặc `control/.env`:
```bash
cp control/.env.example .env
```
Chỉnh sửa các khóa trong `.env`:
- `APP_HOST`: Đặt là `0.0.0.0` để các máy Worker trong LAN truy cập được.
- `APP_PORT`: Cổng Gateway (mặc định `8000`).
- `DATABASE_URL`: Kết nối MySQL (`mysql+pymysql://ai_user:ai_password_change_me@127.0.0.1:3306/ai_team?charset=utf8mb4`).
- `REDIS_URL`: `redis://127.0.0.1:6379/0`.
- `LLM_API_KEY`: API Key OpenAI / Azure / Local.
- `LINEAR_API_KEY` & `LINEAR_TEAM_ID`: Key truy cập Linear.
- `GITHUB_TOKEN` & `GITHUB_REPOSITORY`: Token GitHub thao tác mã nguồn và PR.
- `ADMIN_TOKEN`: Secret Bearer token cho các lệnh điều khiển nhạy cảm.

---

## 4. Thứ tự Khởi động Chuẩn (Bootstrapping Sequence)

Tuân thủ đúng thứ tự 5 bước dưới đây khi khởi động PC1:

### Bước 1: Khởi động Hạ tầng (MySQL 8.4 LTS & Redis)
Chạy lệnh bên trong thư mục `control/`:
```bash
cd control
docker compose up -d
```
Kiểm tra trạng thái healthy của các container:
```bash
docker compose ps
```
> [!NOTE]
> Cả hai container MySQL và Redis đều có cấu hình `healthcheck`. Đảm bảo trạng thái hiển thị `(healthy)` trước khi tiếp tục.

### Bước 2: Kiểm tra Tiêu chuẩn Môi trường
Quay lại thư mục gốc dự án và chạy script kiểm tra:
```bash
python scripts/check_env.py
```
Nếu muốn kiểm tra thêm xác thực token Linear và GitHub:
```bash
python scripts/check_env.py --online
```
> Script phải in ra **"TẤT CẢ HẠNG MỤC ĐẠT CHUẨN!"** và trả về exit code `0`.

### Bước 3: Di chuyển Dữ liệu (Database Migrations - từ S3 trở đi)
Áp dụng cấu trúc bảng MySQL mới nhất:
```bash
alembic upgrade head
```

### Bước 4: Khởi động Control Node Gateway
Chạy ứng dụng FastAPI Gateway trực tiếp trên máy host:
```bash
uvicorn control.app.main:app --host 0.0.0.0 --port 8000 --reload
```

### Bước 5: Xác minh Tình trạng Hoạt động (Health Check)
1. **Kiểm tra cục bộ từ PC1**:
   ```bash
   curl http://127.0.0.1:8000/health
   ```
   Kết quả trả về:
   ```json
   {
     "status": "healthy",
     "node": "control-node",
     "role": "CONTROL",
     "environment": "development",
     "timestamp": "2026-11-04T04:00:00.000000+00:00",
     "version": "1.0.0"
   }
   ```
2. **Kiểm tra từ các máy Worker trong LAN (PC2, PC3, PC4, PC5)**:
   ```bash
   curl http://<IP_PC1_TRONG_LAN>:8000/health
   ```
   Nếu nhận HTTP 200, mạng LAN và Firewall đã thông suốt, sẵn sàng kết nối.

---

## 5. Dừng và Bảo trì Hệ thống
- **Tạm dừng Gateway**: Nhấn `Ctrl + C` trên terminal chạy `uvicorn`.
- **Dừng MySQL & Redis**:
  ```bash
  cd control
  docker compose down
  ```
- **Dừng và xóa toàn bộ dữ liệu volume (Reset DB)**:
  ```bash
  cd control
  docker compose down -v
  ```
