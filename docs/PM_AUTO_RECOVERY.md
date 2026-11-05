# PM tự xử lý phần thiếu và tác vụ bị nghẽn

Luồng managed mặc định bật `PM_AUTO_RECOVERY=true`. Khi thiếu file, thiếu sản phẩm bàn giao, agent lỗi, worker không nhận việc hoặc task quá hạn, PM tự khôi phục/thử lại thay vì để run nằm ở `BLOCKED` chờ người dùng resume.

## Thứ tự xử lý

1. **Khôi phục đúng file cũ.** Artifact Store lưu thêm bản sao theo SHA-256 ở `control/data/artifacts/_recovery/{checksum}`. PM đối chiếu checksum trước khi khôi phục; ID, URI, nội dung và checksum của tài liệu bàn giao giữ nguyên. Nếu file đã lưu nhưng callback thiếu tham chiếu, PM dựng lại tham chiếu từ bản ghi và file thật.
2. **Thử lại tác vụ gốc.** PM sửa đầu vào trước, rồi giao lại việc. Khi phải tạo lại sản phẩm, mọi tác vụ phụ thuộc được đưa về hàng chờ, xóa kết quả hiện hành để làm/kiểm tra lại. Artifact cũ vẫn được lưu; việc bàn giao và xuất release chỉ chọn artifact trong kết quả hiện hành.
3. **PM trực tiếp làm thay.** Sau `PM_RECOVERY_RETRIES` lần khôi phục mà agent vẫn không làm được, PM nhận việc tạo tài liệu hoặc viết/sửa source qua action `recover_task`. PM chạy bằng tiến trình riêng ngay trên Control Node, sử dụng LLM đã cấu hình, không cần worker bị lỗi hoạt động lại. Đầu vào/đầu ra được lưu trong thư mục tạm riêng của từng lần phục hồi; kết quả được đưa về kho artifact chung.
4. **Tiếp tục workflow.** Sản phẩm PM tạo ra phải có đúng loại artifact và tệp thật. Source ZIP được ghép từ toàn bộ source đầu vào cùng các file PM sửa/thêm, giữ nguyên các file còn lại, kể cả file nhị phân. Các bước tester, security, triển khai, nghiệm thu và báo cáo cuối vẫn thực hiện theo workflow.

Phần PM có thể làm thay: backlog, SRS, user stories, kiến trúc, schema, mã nguồn, bộ test case, review module, retrospective, theo dõi blockers và báo cáo tiến độ. Task giữ ID và quan hệ bàn giao; context `recovery_original` ghi vai trò/action/agent ban đầu, còn tác vụ thực tế mang role PM/action `recover_task`.

PM không tự cấp verdict PASS cho tester/security, không tự ký nghiệm thu, duyệt cổng hay giả lập triển khai thật. Các bước thực thi/kiểm tra đó được giao lại cho agent phù hợp. Nếu vòng sửa chất lượng đã đạt giới hạn, PM có thể dùng lại tác vụ tạo source để sửa tiếp và chạy lại các bước phụ thuộc, thay vì thêm task vô hạn.

## Khi dịch vụ ngoài vẫn chưa hoạt động

Nếu LLM/mạng/Linear/kho file vẫn lỗi, run giữ `RUNNING`, `current_stage=pm_recovery`, lưu lý do và `next_retry_at` trong `metadata.recovery`; coordinator tiếp tục thử lại sau thời gian chờ. PM không sinh tài liệu mô phỏng khi chưa có LLM thật và không báo hoàn thành khi chưa có sản phẩm. Người dùng vẫn có thể hủy/tạm dừng; cơ chế khôi phục không tự bật lại run đã bị hủy/tạm dừng.

Backlog Linear vẫn phải đồng bộ xong trước khi giao việc. `AWAITING_APPROVAL` vẫn chờ quyết định đã quy định trong workflow. File thuộc release đã nghiệm thu chỉ được khôi phục đúng bytes theo checksum; không tạo một phiên bản khác rồi ghi đè release cũ. Nếu cả bản chính và bản sao đều mất, PM tiếp tục ghi nhận/thử lại, không tự sửa nội dung của baseline đã nghiệm thu.

## Cấu hình

Trong `control/.env`:

```dotenv
PM_AUTO_RECOVERY=true
PM_RECOVERY_RETRIES=2
PM_RECOVERY_DELAY_SECONDS=5
PM_RECOVERY_MAX_DELAY_SECONDS=60
PM_STALL_SECONDS=60
```

- `PM_RECOVERY_RETRIES`: số lần giao lại trước khi PM làm thay các loại công việc hỗ trợ.
- `PM_STALL_SECONDS`: thời gian worker/agent không nhận được việc trước khi PM bắt đầu xử lý. Task đang chạy dùng deadline của chính task.
- Thời gian chờ tăng dần từ 5 giây và tối đa 60 giây; trạng thái và bộ đếm lưu trong MySQL để tiếp tục sau restart.
- Tắt `PM_AUTO_RECOVERY` để dùng hành vi dừng/chặn như trước. Các kiểm thử fail-fast cũ chủ động tắt cấu hình này.

PM cần LLM thật trong cấu hình PM hoặc `LLM_*` của Control Node để viết code/tài liệu khi làm thay. Sao chép/khôi phục file còn bản dự phòng không cần LLM. Các bản sao được tạo cho artifact lưu sau bản cập nhật này; không bảo đảm khôi phục được file đã mất từ trước.

## Áp dụng và kiểm tra

Cập nhật Contract lên 1.4.0, cài lại package Contract ở Control/Worker/Agent, cập nhật Worker và PM rồi khởi động lại các tiến trình. Mỗi lần giao task managed có `dispatch_token` riêng; Worker mới đưa token vào metadata kết quả để Control bỏ qua callback của lượt cũ và Worker không trả cache cũ cho lượt mới. Callback rất nhanh đến trước ACK cũng không bị mất; việc gửi callback cũ không xóa nhầm kết quả mới khỏi outbox. Không cần migration database riêng cho tính năng này.

```powershell
python -m pip install -e ./contract
$env:PYTHONPATH = 'contract;.'
python -m pytest control/tests/test_project_recovery.py -q
python -m pytest agents/pm/tests/test_pm_agent.py -q
```

Kiểm thử sử dụng SQLite, tệp thật, FakeWorkTracker và LLM giả lập có nội dung cụ thể. Chưa xác minh khả năng sinh code bằng LLM thật hoặc khôi phục trên cụm nhiều máy đang chạy.

Các nhóm đã kiểm tra: 16 tình huống khôi phục, 24 kiểm thử Worker/tiến trình, 16 kiểm thử PM, 58 kiểm thử hồi quy dự án/báo cáo/artifact/Linear và 10 kiểm thử registry/helper/ví dụ Contract. Đây là các nhóm kiểm thử liên quan, không phải toàn bộ test suite của repository.
