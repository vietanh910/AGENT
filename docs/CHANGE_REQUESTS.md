# Chỉnh sửa dự án đã hoàn thành

Một `project_id` có nhiều đợt thực hiện (`run_id`) và nhiều phiên bản đã nghiệm thu. Đợt chỉnh sửa kế thừa **phiên bản hiện tại**, không tạo lại bộ khung dự án. Project trên Linear được dùng chung; mỗi đợt có milestone và task mới.

## Cập nhật database một lần

Từ thư mục gốc `AGENT`, dùng môi trường Python và `DATABASE_URL` của Control Node đang vận hành:

```powershell
python -m alembic -c control/alembic.ini upgrade head
```

Migration `002_project_releases` bổ sung `projects` và `project_releases`, không sửa hay xóa task/artifact cũ. Lệnh dùng `DATABASE_URL` từ môi trường shell; cần đặt đúng URL nếu khác giá trị mặc định trong `control/alembic.ini`. Sau đó khởi động lại Control Node và PM để nạp mã mới. Không tự chạy migration vào database thật trong quá trình kiểm thử mã nguồn.

## Gửi yêu cầu bằng câu mô tả

Từ thư mục `agents/pm`, với cấu hình managed trong [MODULE_PROJECTS.md](MODULE_PROJECTS.md):

```powershell
python cli.py project change LIB-01 --request "Thêm gia hạn mượn sách tối đa hai lần, giữ nguyên các chức năng hiện có"
```

Thay `LIB-01` bằng **project_id** nhận được khi tạo dự án ban đầu. PM đọc các module, SRS và tài liệu thiết kế của phiên bản đã bàn giao để đề xuất phạm vi thay đổi. Bạn không cần tự lập file JSON.

Kết quả có `run_id` mới dạng `CHG-...`, `base_run_id`, `base_release_id` và `target_version`. Dùng run_id mới để theo dõi và duyệt công việc:

```powershell
python cli.py project status CHG-... --watch
python cli.py project approve CHG-... --gate APPROVE_PLAN
python cli.py project approve CHG-... --gate approve_scope
```

Các cổng duyệt hiện có vẫn áp dụng theo cấu hình Control Node. PM đồng bộ đủ kế hoạch lên Linear trước khi giao việc. Nếu việc lập kế hoạch gặp lỗi hoặc dùng LLM mock, kế hoạch dự phòng bắt buộc chờ xem xét.

Muốn chỉ định chính xác module/task hoặc ảnh hưởng đến database/kiến trúc:

```powershell
python cli.py project change LIB-01 --request-file change.md --module-plan examples/library_change_plan.json
```

`module_id` của module cần sửa phải dùng đúng ID đã có; ID mới thể hiện module mới. Chỉ liệt kê module bị ảnh hưởng. `architecture_changed` và `database_changed` quyết định việc giao thêm task cho SA/DBA. Nếu thay đổi database, task DBA yêu cầu cung cấp migration bảo toàn dữ liệu và phương án rollback.

## PM thực hiện những gì

1. Xác định phiên bản hiện tại và cố định các artifact bằng ID/checksum. Kiểm tra tệp gốc còn tồn tại và đúng checksum trước khi lập kế hoạch.
2. Cập nhật backlog, SRS và user stories; giữ nội dung không bị ảnh hưởng.
3. Cập nhật kiến trúc/database khi kế hoạch chỉ ra có ảnh hưởng, rồi sửa SRS và bộ test của các module được chọn. Bộ test chung cũng được cập nhật theo SRS mới để các kỳ vọng cũ đã được thay đổi hợp lệ không gây báo lỗi hồi quy sai.
4. DEV sửa **source đã bàn giao**, trả lại toàn bộ source tích hợp. Các task tiếp theo nhận source vừa hoàn thành hoặc vừa sửa lỗi.
5. TESTER kiểm tra từng task và chạy bộ hồi quy cuối cùng: test mới của module sửa cùng test kế thừa của các module không sửa. Security và nghiệm thu tiếp tục theo quy trình hiện có.
6. Khi toàn bộ đợt đạt nghiệm thu, tạo manifest phiên bản mới và cập nhật con trỏ phiên bản hiện tại trong cùng transaction. Khi thất bại, phiên bản hiện tại vẫn giữ nguyên.

Ví dụ phiên bản v1 có danh mục và mượn trả; yêu cầu gia hạn tạo v2. Mã nguồn v2 có cả danh mục, mượn trả và gia hạn. Tài liệu/test của danh mục được kế thừa; không giao DEV xây dựng lại module danh mục. Các tệp và lịch sử task của v1 vẫn được giữ.

## Lịch sử phiên bản và API

```powershell
python cli.py project history LIB-01
```

Các endpoint Control Node (header `Authorization: Bearer <ADMIN_TOKEN>`):

| Endpoint | Chức năng |
|---|---|
| `GET /api/v1/projects` | Danh sách dự án và phiên bản hiện tại |
| `GET /api/v1/projects/{project_id}` | Phiên bản hiện tại và đợt sửa đang hoạt động |
| `POST /api/v1/projects/{project_id}/changes` | Tạo yêu cầu chỉnh sửa |
| `GET /api/v1/projects/{project_id}/changes` | Lịch sử yêu cầu, gồm cả đợt thất bại |
| `GET /api/v1/projects/{project_id}/releases` | Các phiên bản đã nghiệm thu |
| `GET /api/v1/projects/{project_id}/releases/{release_id}` | Manifest artifact của một phiên bản |

Body tạo yêu cầu:

```json
{
  "brief": "Thêm gia hạn mượn sách tối đa hai lần",
  "title": "Gia hạn mượn sách"
}
```

Có thể thêm `module_plan` theo mẫu và `base_run_id` để xác nhận phiên bản bạn đang dựa vào. Nếu base_run_id đã cũ, server trả `409` thay vì tự thay thế phiên bản mới hơn. Mỗi dự án có tối đa một đợt chỉnh sửa đang hoạt động; muốn gửi đợt khác, hoàn tất hoặc hủy đợt hiện tại.

Qua PM API, dùng `POST /projects/{project_id}/changes` và `GET /projects/{project_id}/releases`, với Bearer token của PM. PM chuyển tiếp sang Control Node bằng `CONTROL_ADMIN_TOKEN` như cấu hình managed. Các thao tác status/approve/resume/abort vẫn dùng **run_id**, còn change/history dùng **project_id**.

Nếu Control Node khởi động lại khi đang lập kế hoạch chỉnh sửa, vòng điều phối tìm lại run `PLANNING` và tiếp tục. Khóa hàng run và kiểm tra task đã lưu ngăn tạo backlog trùng khi tác vụ nền được gọi lại.

## Dự án có trước lần cập nhật này

Lần yêu cầu chỉnh sửa đầu tiên tự ghi nhận phiên bản v1 từ run managed đã hoàn thành gần nhất của dự án. Run đó phải có đầy đủ artifact, kết quả kiểm thử/nghiệm thu và tệp đúng checksum trong Artifact Store. Không thể tự kế thừa chỉ từ các tài liệu mẫu trong `deliverables/` hoặc một dự án chưa hoàn tất.

`resume` không mở lại run đã hoàn thành. Để sửa sản phẩm đã nghiệm thu, luôn tạo một yêu cầu thay đổi mới. Cơ chế này không tự triển khai hay khôi phục production khi thay đổi lỗi; các chính sách triển khai hiện có vẫn quyết định việc thực thi.
