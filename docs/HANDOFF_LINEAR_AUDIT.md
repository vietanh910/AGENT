# Kiểm tra bàn giao sản phẩm và trạng thái Linear

Ngày kiểm tra: 2026-11-05. Kiểm tra mã nguồn và chạy kiểm thử; không ghi dữ liệu vào tài khoản Linear thật.

## Kết quả bàn giao

Luồng đã có và đã được kiểm thử: **Agent trước → Worker upload → Artifact Store trên Control Node → PM chọn đầu vào theo task nguồn → Worker sau tải vào inbox → Agent sau thực hiện**.

- Worker chỉ gửi callback hoàn thành sau bước upload sản phẩm của task.
- PM/Control Node chọn artifact theo task sản xuất và loại tài liệu. SRS chung và SRS module được dùng lại; TESTER nhận đúng code từ task DEV tương ứng.
- Worker nhận việc tải file trước khi chạy agent, kiểm tra SHA-256 với TaskRequest và header server. File lỗi/thiếu hoặc sai checksum làm tác vụ bị chặn.
- Danh sách `input_artifact_sources` trong task và `TaskRequest.input_artifacts` thể hiện tài liệu được bàn giao. Đợt chỉnh sửa còn kiểm tra nguồn cố định trong release gốc.

PM là bộ điều phối; Worker thực hiện việc upload/download file, thay vì tiến trình PM tự sao chép trực tiếp giữa hai agent.

## Kết quả cập nhật Linear

Sau callback, chu kỳ điều phối đồng bộ trạng thái task sang Linear. Cấu hình trong `control/linear.yaml` ánh xạ `COMPLETED` sang **Done**. Bộ đồng bộ gọi mutation cập nhật đúng issue; client kiểm tra API có xác nhận đúng issue ID rồi mới ghi nhận đồng bộ thành công.

Task có sản phẩm không hợp lệ, thiếu verdict kiểm thử/bảo mật hoặc verdict FAIL chưa được khắc phục không được coi là Done. Khi chưa đồng bộ được hoặc chưa xác minh xong sản phẩm, task sau không được giao chạy.

## Lỗi phát hiện và đã sửa trong lần kiểm tra này

1. Trước đây, vòng đồng bộ trạng thái có thể chuyển task sang Done trước bước kiểm tra sản phẩm. Một test tái hiện đã xác nhận trường hợp thiếu BACKLOG vẫn có `state_id=st-done`. Đã đổi thứ tự: kiểm tra sản phẩm và verdict trước khi chọn trạng thái đồng bộ. Task không đạt chuyển sang trạng thái bị chặn trên Linear, kèm lý do.
2. Worker có thể bỏ sót file khai báo trong TaskResult nếu index.json chỉ chứa một phần sản phẩm; file không tồn tại còn bị bỏ qua. Đã dùng đầy đủ danh sách sản phẩm khai báo, đối chiếu kết quả upload bằng artifact ID và trả FAILED khi thiếu file.
3. Client Linear trước đây chỉ gọi mutation mà không kiểm tra issue trả về; mapping trạng thái thiếu còn có thể bị tính là đồng bộ thành công. Đã yêu cầu phản hồi xác nhận và không ghi thành công khi thiếu mapping.

Kiểm thử kiểm tra tất cả task đã hoàn thành hợp lệ trong một dự án đều có trạng thái Done trên FakeWorkTracker, SRS/code được chọn đúng nguồn và đúng checksum, thiếu sản phẩm/verdict không bị Done, upload đủ file khi index thiếu và từ chối phản hồi Linear không xác nhận cập nhật.

## Phạm vi xác minh thực tế

- `127.0.0.1:8000/health` và `127.0.0.1:8001/health` không kết nối được khi kiểm tra.
- Cấu hình đọc được tại workspace: `LINEAR_DRY_RUN=true`, chưa có API key Linear thật; không có `control/.env` và `agents/pm/.env`.
- Vì vậy, kết quả xác nhận luồng mã nguồn và mô phỏng, **chưa xác nhận task cụ thể trên tài khoản Linear hoặc máy Control Node khác đã chuyển Done**.

94 kiểm thử liên quan đạt: 66 test về điều phối, bàn giao, thay đổi dự án, Linear và lưu trữ; 15 test Worker; 13 test PM. Có thêm kiểm tra cảnh báo rủi ro nghiệp vụ không bị nhầm thành lỗi kiểm thử để mở lại task đã xong. Chi tiết nhóm kiểm thử và giới hạn môi trường: [MODULE_PROJECTS_VALIDATION.md](MODULE_PROJECTS_VALIDATION.md).
