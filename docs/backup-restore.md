# Hướng dẫn Vận hành Sao lưu và Phục hồi Cơ sở dữ liệu (docs/backup-restore.md)

Tài liệu hướng dẫn quy trình vận hành sao lưu định kỳ, phòng ngừa rủi ro và khôi phục sau thảm họa cho Control Node PC1 của hệ thống **ai-team**.

---

## 1. Chiến lược sao lưu (Backup Strategy)

- **Công cụ sao lưu**: `mysqldump` với tùy chọn `--single-transaction --quick --routines --triggers`.
- **Cơ chế nhất quán (Consistency)**:
  - Tùy chọn `--single-transaction` sử dụng Transaction Isolation Level `REPEATABLE READ` của InnoDB.
  - Cho phép trích xuất snapshot dữ liệu nhất quán tại một thời điểm mà **không khóa bảng (non-locking)**, đảm bảo luồng Dispatcher, Worker callback và API hoạt động bình thường không bị gián đoạn.
- **Tần suất thực hiện**:
  - Tự động chạy hàng ngày vào lúc 02:00 AM (giờ hệ thống).
  - Tự động sao lưu trước các đợt bảo trì nâng cấp lớn (Schema Migration).
- **Chính sách lưu trữ (Retention Policy)**:
  - Giữ lại tối thiểu **N = 7** bản sao lưu gần nhất trên đĩa cứng cục bộ (`control/data/backups/`).
  - Tự động dọn dẹp các bản sao lưu cũ hơn khi số lượng vượt quá ngưỡng quy định.

---

## 2. Hướng dẫn chạy Sao lưu (scripts/backup.py)

### 2.1 Lệnh chạy thủ công
```bash
# Sử dụng cấu hình mặc định trong .env
python scripts/backup.py

# Hoặc chỉ định database URL và số bản sao lưu cần giữ
python scripts/backup.py --url "mysql+pymysql://ai_user:password@127.0.0.1:3307/ai_team" --keep 14
```

### 2.2 Cấu hình lịch tự động

#### Trên Linux / macOS (Crontab)
Chạy lệnh `crontab -e` và thêm dòng sau:
```cron
# Chạy sao lưu hàng ngày lúc 02:00 sáng, ghi log vào backup.log
0 2 * * * cd /path/to/AGENT && /path/to/venv/bin/python scripts/backup.py >> /var/log/ai-team-backup.log 2>&1
```

#### Trên Windows (Task Scheduler / PowerShell)
Mở PowerShell với quyền Administrator và tạo Scheduled Task:
```powershell
$Action = New-ScheduledTaskAction -Execute "python.exe" -Argument "scripts\backup.py" -WorkingDirectory "C:\Users\Admin\Desktop\qldapm-4(5)\AGENT"
$Trigger = New-ScheduledTaskTrigger -Daily -At 2:00AM
Register-ScheduledTask -TaskName "AITeamDatabaseBackup" -Action $Action -Trigger $Trigger -Description "Daily MySQL Backup for AI Team Control Node"
```

---

## 3. Hướng dẫn Phục hồi (scripts/restore.py)

### 3.1 Phục hồi vào Database hiện hành (Disaster Recovery)
> [!CAUTION]
> Thao tác này sẽ ghi đè toàn bộ dữ liệu trong database hiện tại bằng dữ liệu từ bản sao lưu. Đảm bảo dừng các tiến trình Orchestrator trước khi phục hồi.

```bash
python scripts/restore.py control/data/backups/backup_ai_team_20261104_105548.sql
```

### 3.2 Phục hồi thử nghiệm trên Database trống (Verification / Drill)
Để kiểm tra tính toàn vẹn của bản sao lưu mà không làm ảnh hưởng đến môi trường hoạt động chính:
```bash
python scripts/restore.py control/data/backups/backup_ai_team_20261104_105548.sql --target-db ai_team_test_restore
```

Script sẽ tự động:
1. Nhận diện Docker container hoặc máy chủ MySQL cục bộ.
2. Nạp schema và toàn bộ bản ghi.
3. Kiểm tra số lượng bảng và số lượng bản ghi của 16 bảng cốt lõi (`tasks`, `workflow_runs`, `audit_logs`, `agents`, `workers`, ...).
4. Xuất kết quả xác minh `RESTORE_SUCCESS`.

---

## 4. Kiểm tra Toàn vẹn Chuỗi Audit Log sau Phục hồi

Hệ thống ghi nhận Audit Log có bảo vệ bằng cơ chế băm Merkle Hash Chain (`prev_hash` và `entry_hash`). Sau khi phục hồi dữ liệu, người quản trị có thể kiểm tra tính toàn vẹn dữ liệu chống can thiệp bằng script Python:

```python
from control.app.database.session import get_db_session
from control.app.observability.audit import AuditService

with get_db_session() as session:
    valid, corrupted_id = AuditService.verify_chain_integrity(session)
    if valid:
        print("CHUỖI AUDIT LOG TOÀN VẸN VÀ KHÔNG BỊ CAN THIỆP.")
    else:
        print(f"CẢNH BÁO: Phát hiện bản ghi audit log bị sửa đổi tại ID: {corrupted_id}")
```
