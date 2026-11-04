# Báo cáo Triển khai Dịch vụ (STAGING)

- **Dịch vụ:** web-service
- **Phiên bản:** web-service:v1.0
- **Trạng thái:** DRY_RUN
- **URL truy cập:** http://127.0.0.1:8102

## Kế hoạch Rollback dự phòng
- `docker compose down`
- `docker compose -f rollback.yml up -d`

## Log ghi nhận
```
[DRY_RUN] Lệnh dự kiến thực thi: docker compose -p staging_task_09_devops_01_7e1973 up -d (tại C:\Users\Admin\Desktop\qldapm-4(5)\AGENT\deploy\staging\TASK-09-DEVOPS-01-7e1973)
```