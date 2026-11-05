# Dashboard realtime trên Control Node

Dashboard nằm ngay trong ứng dụng Control Node, tại `/dashboard`. Trang đọc trạng thái vận hành đã lưu và nhận cập nhật qua Server-Sent Events (SSE), khoảng mỗi giây. Không có dữ liệu mẫu trong giao diện chạy thật.

## Mở dashboard

Khởi động lại Control Node sau khi cập nhật source. Nếu cần chạy từ đầu, dùng môi trường Python đang chạy Control Node:

```powershell
cd D:\ltm\AGENT
python -m uvicorn control.app.main:app --host 0.0.0.0 --port 8000
```

Mở **http://127.0.0.1:8000/dashboard** trên máy Control Node. Máy khác trong LAN/Radmin VPN dùng `http://<IP-Control-Node>:8000/dashboard`, theo cấu hình kết nối/port của hệ thống.

Nhập `ADMIN_TOKEN` từ `control/.env`. Giao diện chỉ giữ mã trong bộ nhớ của tab hiện tại; tải lại trang hoặc ngắt kết nối cần nhập lại. Token được gửi qua header Authorization, không đặt trong URL. Token Worker không được xem dữ liệu dashboard hoặc tải file qua API dashboard.

MySQL và schema hiện có cần hoạt động để đọc dữ liệu. Khi chưa có project/task/worker, trang hiển thị trạng thái trống hoặc chưa kết nối. Tính năng này không cần migration mới hoặc tiến trình frontend riêng.

## Thông tin hiển thị

- **Agent:** vai trò, máy thực hiện, trạng thái, công việc hiện tại. PM làm thay tại Control Node được nhận diện từ task thực tế, kể cả khi worker PM không hoạt động.
- **Máy/worker:** online, offline hoặc chưa đăng ký, dựa trên trạng thái và heartbeat gần nhất. Agent chưa đăng ký không được hiển thị là sẵn sàng chỉ vì một worker khác cùng máy đang online.
- **Dự án:** chọn run để xem tên, trạng thái, tiến độ, task và các quan hệ phụ thuộc từ database. Chọn task để đọc chỉ dẫn, tiêu chí nghiệm thu, số lần thực hiện/khôi phục, nguồn artifact và file đầu ra.
- **Kiểm thử và phục hồi:** task có verdict FAIL/chưa có verdict hoặc lỗi bàn giao được hiển thị đang bị chặn; không tính hoàn thành cho đến khi được xử lý/kiểm tra lại. Banner hiển thị lý do và lịch thử lại của PM.
- **File bàn giao:** tên, loại, agent gửi, máy gửi, task/module, thời điểm nhận, dung lượng, đường dẫn tuyệt đối và tình trạng file trên ổ đĩa. File mới có thông báo ngay khi snapshot tiếp theo đọc được bản ghi đã commit.
- **Nhật ký:** sự kiện agent/worker, khôi phục, nghiệm thu và file nhận về, lấy từ các bản ghi đã lưu. Giao diện không xuất toàn bộ payload hoặc cấu hình bí mật của sự kiện.

Sơ đồ thể hiện quan hệ phụ thuộc thực tế của các task; thứ tự được sắp theo DAG. Nhãn **Chờ worker** phân biệt tình trạng task còn được ghi đang chạy nhưng máy thực hiện đã mất kết nối. Các agent được theo dõi trên toàn hệ thống; sơ đồ và tiến độ thuộc run đang chọn.

## Đường dẫn file là trên máy nào?

Mục kho file luôn ghi hostname và thư mục gốc, ví dụ:

```text
Máy lưu: ADMIN-PC — Control Node
D:\ltm\AGENT\control\data\artifacts\RUN-...\ART-...-source.zip
```

Đây là vị trí file được agent upload về **máy chạy Control Node**. Nếu mở dashboard từ máy khác, đường dẫn đó vẫn thuộc Control Node. Nút sao chép chép chính đường dẫn này; nút tải tạo một bản sao theo vị trí tải xuống của trình duyệt trên thiết bị đang mở dashboard.

File mới upload từ Worker ghi `source_worker_id` và `source_machine` từ danh tính Worker đã xác thực. Worker chỉ được nhận là người gửi cho các agent thuộc quyền của mình. Với file cũ chưa có metadata nguồn máy, dashboard dùng thông tin máy hiện tại của agent; `created_by` vẫn lấy từ bản ghi artifact.

Nếu file bị mất, dashboard vẫn giữ bản ghi và đường dẫn dự kiến, đánh dấu thiếu file và tắt nút tải. Khi tải, server đối chiếu checksum với bản ghi trước khi trả nội dung. Trạng thái khôi phục file cập nhật theo lần đọc dữ liệu tiếp theo.

Có bộ lọc theo agent, ô tìm tên/file/task/đường dẫn, và phạm vi run hoặc toàn hệ thống. Danh sách run giới hạn 100 run gần nhất; bảng file hiển thị 200 file gần nhất của run hoặc 80 file gần nhất toàn hệ thống. Tổng số file ở phần thống kê là tổng bản ghi artifact trong kho, không phải số hàng đang hiển thị. Bảng và sơ đồ có thể cuộn ngang trên màn hình nhỏ.

## Realtime và khi mất kết nối

- Browser gọi API cùng origin, mở luồng SSE bằng fetch có Authorization header.
- Mỗi vòng đọc dùng một session DB riêng rồi đóng ngay; không giữ transaction qua thời gian chờ giữa các lần cập nhật.
- Snapshot chỉ được gửi lại khi revision thay đổi; khi chưa có thay đổi, server gửi heartbeat.
- Mất mạng, luồng bị ngắt hoặc database lỗi được hiển thị rõ; dữ liệu lần trước có thể được giữ để tham khảo. Dashboard tự kết nối lại, không tự đánh dấu task hoàn thành.
- Khôi phục database gửi snapshot mới kể cả dữ liệu không đổi so với trước gián đoạn. Lệnh ngắt kết nối xóa token và dữ liệu hiển thị của phiên làm việc.
- Nếu phía trước Control có reverse proxy, cần cho phép streaming và tắt buffering cho `/api/v1/dashboard/stream`. API đã gửi `X-Accel-Buffering: no`.

## API và mã nguồn

Các endpoint dashboard yêu cầu quyền Admin:

```text
GET /api/v1/dashboard/snapshot?run_id=RUN-...
GET /api/v1/dashboard/stream?run_id=RUN-...
GET /api/v1/dashboard/artifacts/{artifact_id}/content
```

Trang HTML và asset tĩnh công khai chỉ chứa giao diện đăng nhập; dữ liệu thực tế nằm sau API xác thực.

- `control/app/services/dashboard.py`: tổng hợp trạng thái, DAG, file và đường dẫn.
- `control/app/api/dashboard.py`: snapshot, SSE và tải file.
- `control/app/dashboard_ui.py`: phục vụ trang/asset từ Control Node.
- `control/app/static/dashboard/`: HTML, CSS và JavaScript, không dùng CDN.

## Kiểm tra

```powershell
$env:PYTHONPATH = 'contract;.'
python -m pytest control/tests/test_dashboard.py -q
```

Kiểm thử backend dùng SQLite và file thật: xác thực, heartbeat hết hạn, PM làm thay cục bộ, task kiểm thử FAIL, nguồn máy gửi đã xác thực, đường dẫn tuyệt đối, file mất/sai checksum, sự kiện mới và stream tự hồi phục khi database lỗi.

Đã kiểm tra thêm bằng Chrome headless ở desktop và mobile với ứng dụng QA sử dụng database thử nghiệm riêng: tải file mới qua HTTP rồi nhận trên trang bằng SSE, sao chép đúng đường dẫn, tải đúng bytes, tìm kiếm, chuyển run, chi tiết task và mất mạng/kết nối lại. Chưa kiểm tra trên MySQL và cụm năm máy chạy thật.
