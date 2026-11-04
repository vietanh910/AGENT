# KẾ HOẠCH DỰ ÁN: TODO LIST APP (MULTI-AGENT SYSTEM)

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
