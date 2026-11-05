# AGENT - AI Team Monorepo

Luồng PM mới: [quản lý dự án theo module/task trên Linear](docs/MODULE_PROJECTS.md), bàn giao artifact và kiểm thử sau từng task.

[Chỉnh sửa dự án đã hoàn thành](docs/CHANGE_REQUESTS.md): kế thừa code/tài liệu từ phiên bản hiện tại, tạo đợt công việc mới trong cùng dự án Linear và lưu lịch sử phiên bản.

[Báo cáo tổng kết Word](docs/PROJECT_REPORT.md): tự tạo file `.docx` khi hoàn tất dự án hoặc một phiên bản chỉnh sửa, tổng hợp tài liệu của các agent và cho phép tải về đọc.

[PM tự khôi phục công việc](docs/PM_AUTO_RECOVERY.md): khôi phục file, thử lại tác vụ và trực tiếp tạo tài liệu/code thay agent bị nghẽn; lưu tiến độ xử lý qua restart.

[Dashboard realtime](docs/REALTIME_DASHBOARD.md): mở `/dashboard` trên Control Node để theo dõi agent, máy, luồng task và file nhận về cùng đường dẫn lưu thực tế.

Hệ thống 10 AI Agent độc lập xây dựng bởi đội ngũ 5 kỹ sư:
PM, SA, BA, PO, DEV, TESTER, DEVOPS, DBA, SCRUM_MASTER, SECURITY.

## 1. Cấu trúc thư mục
- `contract/`: Gói Python dùng chung `agent_contract` chứa toàn bộ Enums, Pydantic Models, Registry, Validators và JSON Schemas.
- `docs/`: Tài liệu đặc tả `CONTRACT.md` và lịch sử phiên bản `CHANGELOG.md`.
- `agent-template/`: Mẫu Agent để clone và phát triển.
- `agents/`: Thư mục chứa mã nguồn của từng agent (`pm`, `devops`, `sa`, ...).
- `tools/`: Các công cụ hỗ trợ (như `validate_agent.py`).

## 2. Quy tắc phát triển
1. Mọi agent tuân theo Contract v1.0, I/O sử dụng Pydantic từ gói `agent_contract`.
2. Không tự sửa protocol; muốn đổi phải thảo luận và tăng version contract.
3. Không hard-code API key hay LLM provider; không commit `.env`.
4. Agent chỉ thực hiện action thuộc capability đã khai báo trong `CAPABILITY_REGISTRY`.
5. Agent không gọi trực tiếp agent khác; chỉ gợi ý qua `next_actions`.
6. Mọi ngoại lệ bên trong agent phải được bắt và trả lời dưới dạng `TaskResult` kèm `AgentError`. Không ném exception ra ngoài `handle()`.
7. Mọi agent phải có unit test chạy offline bằng mock LLM.

## 3. Cài đặt Contract
```bash
pip install -e ./contract
```
Chạy test contract:
```bash
python -m pytest contract -v
```
Xuất JSON Schema:
```bash
python -m agent_contract.export_schemas
```

## 4. Hướng dẫn kết nối mạng LAN & Thiết lập nhiều máy (Multi-Machine)
Xem tài liệu hướng dẫn chi tiết tại [`docs/CONNECTIVITY_GUIDE.md`](docs/CONNECTIVITY_GUIDE.md):
- Bảng phân bổ 10 Agent trên 5 máy (cổng 8001 - 8010).
- Tiện ích thiết lập nhanh: `python scripts/configure_lan.py setup-machine <1..5>`.
- Kiểm tra kết nối mạng LAN: `python scripts/lan_check.py`.
