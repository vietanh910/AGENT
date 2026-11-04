# Security Report

- **Task:** TASK-TRIAL-08-1793813125
- **Project:** PRJ-TRIAL-TODO
- **Verdict:** **FAIL**
- **Tóm tắt:** FAIL: 0 CRITICAL, 2 HIGH, 0 MEDIUM, 0 LOW trên 2 file (công cụ: rules).

## Thống kê

| Mức độ | Số lượng |
|---|---|
| CRITICAL | 0 |
| HIGH | 2 |
| MEDIUM | 0 |
| LOW | 0 |
| INFO | 0 |
| files_scanned | 2 |

## Findings

| Mức độ | Rule | Vị trí | Mô tả | Khắc phục |
|---|---|---|---|---|
| HIGH | SEC-SECRET | `calc.py:2` | Secret / mật khẩu bị hardcode: `PASSWORD = 'hunter2hunter2'` | Đọc secret từ biến môi trường hoặc secret manager. |
| HIGH | SEC-SHELL | `calc.py:4` | subprocess với shell=True (command injection): `subprocess.run('ls', shell=True)` | Truyền list tham số, bỏ shell=True. |
