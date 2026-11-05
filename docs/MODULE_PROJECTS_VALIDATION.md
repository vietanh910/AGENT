# Kiểm chứng luồng dự án theo module

Ngày kiểm tra: 2026-11-05. Python 3.12.13 trong môi trường kiểm thử tạm; không sửa các file `.env` đang có.

| Nhóm | Kết quả |
|---|---|
| `control/tests/test_change_requests.py` | 17 đạt |
| `control/tests/test_module_projects.py` và `worker/tests/test_process_agent.py` | 21 đạt |
| Artifact Store, tích hợp Linear, PlanValidator, retry/fallback/song ngữ và endpoint health | 28 đạt |
| `agents/pm/tests/test_pm_agent.py` | 13 đạt |
| Worker artifact workflow và Worker Host hiện có | 15 đạt |
| `git diff --check` | Đạt |

Tổng cộng 94 kiểm thử thuộc các nhóm được chọn đã đạt. Kiểm thử dùng SQLite/FakeWorkTracker, HTTP mock và tiến trình PM thật với LLM mock. Đã kiểm tra tạo toàn bộ backlog trước khi giao việc, chặn khi Linear lỗi, thứ tự module/task, nguồn SRS/code, sửa và test lại, rà soát bảo mật lại, giới hạn sửa lỗi, khôi phục coordinator, báo cáo từ tiến độ thật, file trùng tên và ID artifact giữa các task.

Kiểm tra riêng về bàn giao và trạng thái Done: [HANDOFF_LINEAR_AUDIT.md](HANDOFF_LINEAR_AUDIT.md). Đã bổ sung các ca thiếu file, index thiếu sản phẩm, thiếu verdict, xác nhận mutation Linear và kiểm tra trạng thái Done của từng task.

Luồng chỉnh sửa đã được kiểm tra từ v1 lên v2 và tiếp tục v3, giữ nguyên dữ liệu/tệp của phiên bản cũ và cùng Project Linear, chỉ lập task cho module bị ảnh hưởng, kế thừa đúng source theo checksum, hồi quy các module không sửa, cập nhật test chung, sửa lỗi bảo mật rồi kiểm thử/rà soát lại, không đổi phiên bản hiện tại khi sửa thất bại, từ chối nguồn cũ hoặc nguồn bị thay đổi, khôi phục đợt đang lập kế hoạch và ngăn xuất bản trùng sau khi chờ khóa. Migration được kiểm tra trên SQLite với khóa ngoại, bao gồm gọi upgrade lại và downgrade trong database kiểm thử riêng. PM API/CLI đã được kiểm tra thao tác change/history và giữ mã lỗi 409 từ Control Node.

Khi mở rộng chạy `contract/tests`, bộ kiểm thử dừng sau 24 test đạt tại `TestAgentIdRegexValidation.test_valid_agent_ids`: test đưa `qa_lead-99` vào danh sách ID hợp lệ, nhưng validator chỉ chấp nhận tiền tố thuộc AgentRole và không có `qa_lead`. Các file test/validator/model liên quan không được thay đổi trong lần triển khai này. Chưa chạy hết bộ Contract sau lỗi đó.

Chưa xác minh với MySQL/Redis đang vận hành, Linear thật, LLM thật hoặc các máy Worker trong LAN. Các test Worker hiện có vẫn ghi cảnh báo callback nền đến địa chỉ mock không tồn tại sau khi kiểm thử; kết quả test và SQLite outbox đã được kiểm tra thành công.

Cách cấu hình và chạy thật: [MODULE_PROJECTS.md](MODULE_PROJECTS.md). Chỉnh sửa sau bàn giao và migration mới: [CHANGE_REQUESTS.md](CHANGE_REQUESTS.md).
