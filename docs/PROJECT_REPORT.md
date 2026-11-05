# Báo cáo tổng kết dự án dạng Word

Luồng **managed** tự tạo `project-report-v1.docx` sau khi hoàn tất các module/task, kiểm thử, bảo mật, triển khai, nghiệm thu của PO và retrospective. Control Node tổng hợp tài liệu đã lưu, xuất Word và gắn vào sản phẩm của task PM `report_status`. PM chịu trách nhiệm tổng kết; không cần thêm một agent mới.

File Word phải được lưu thành công trước khi run chuyển sang `COMPLETED` và phiên bản mới trở thành phiên bản hiện hành. Nếu không ghi được báo cáo hoặc tài liệu nguồn bị mất/sai checksum, phiên bản hiện hành được giữ nguyên. Khi bật [PM tự khôi phục](PM_AUTO_RECOVERY.md) (mặc định), PM khôi phục file/thử lại và giữ run ở `RUNNING` với lý do, lịch thử lại trong `metadata.recovery`. Khi tắt cơ chế này, run chuyển sang `BLOCKED` và cần resume sau khi khắc phục.

## Nội dung

1. Mục tiêu, phạm vi và yêu cầu thay đổi của phiên bản.
2. Toàn bộ module, chức năng và tiêu chí nghiệm thu, gồm phần kế thừa không sửa.
3. Lịch sử phiên bản, task, agent phụ trách và trạng thái thực hiện.
4. Backlog, SRS và user stories.
5. Kiến trúc và công nghệ.
6. Thiết kế cơ sở dữ liệu.
7. Mã nguồn và hướng dẫn cài đặt/sử dụng lấy từ README, tài liệu trong gói source.
8. Test case, báo cáo kiểm thử, bảo mật, verdict và các lần sửa/kiểm tra lại.
9. Các tài liệu CI, build, deploy và health check của đợt bàn giao.
10. Cổng phê duyệt, nghiệm thu, review module, retrospective và báo cáo tiến độ.
11. Giới hạn dữ liệu được tổng hợp và đề xuất trong tài liệu của các agent.
12. Danh sách artifact bàn giao, nguồn, đường dẫn tải và checksum.

Báo cáo đọc tài liệu thực tế, không dùng LLM để đoán kết quả. Hỗ trợ văn bản/Markdown, JSON, YAML, SQL, CSV, log, Word `.docx`; gói ZIP chỉ đọc README và tài liệu Markdown/text trong thư mục `docs`, không giải nén hay thực thi mã nguồn. Tệp ngoài định dạng hỗ trợ được liệt kê để tải riêng. Nếu tài liệu không có hướng dẫn sử dụng hoặc chưa ghi nhận hạn chế, báo cáo không tự tạo thông tin thay thế.

Mỗi tệp nguồn đọc tối đa 2 MB, mỗi phần trích tối đa 24.000 ký tự và tổng phần trích tối đa 350.000 ký tự. Các đoạn rút gọn, tệp không đọc được và trường hợp dữ liệu cũ chỉ có metadata đều được ghi rõ trong báo cáo. Artifact gốc vẫn được giữ để đọc đầy đủ.

## Tải báo cáo

Control Node cần cài lại dependency khi cập nhật:

```powershell
python -m pip install -r control/requirements.txt
```

Khởi động lại Control Node để nạp mã mới. Không cần thêm migration cho riêng tính năng báo cáo. API tải yêu cầu xác thực như các API đọc dự án khác:

```http
GET /api/v1/projects/{project_id}/report
Authorization: Bearer <ADMIN_TOKEN>
```

Tải bằng PowerShell, dùng project ID ổn định lấy từ thông tin run:

```powershell
$projectId = 'LIB'
$headers = @{ Authorization = "Bearer $env:ADMIN_TOKEN" }
Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/v1/projects/$projectId/report" `
  -Headers $headers -OutFile '.\bao-cao-du-an.docx'
```

API trả file Word trực tiếp, kèm `X-Checksum-SHA256`. Dự án chưa có phiên bản nghiệm thu trả `409`. Nếu đang thực hiện thay đổi, đường dẫn trên vẫn trả báo cáo của phiên bản hiện hành đã nghiệm thu.

Mỗi lần chỉnh sửa dự án hoàn tất sẽ tạo `project-report-v2.docx`, `project-report-v3.docx`… Báo cáo mới tổng hợp cả chức năng kế thừa và thay đổi, giữ nguyên báo cáo cũ. Đọc `GET /api/v1/projects/{project_id}/releases` để lấy trường `report.download_url`, hoặc tải theo phiên bản:

```http
GET /api/v1/projects/{project_id}/releases/{release_id}/report
Authorization: Bearer <ADMIN_TOKEN>
```

Thông tin file có trong `run.metadata.project_report`, `release.manifest.report`, danh sách artifact của run và kết quả task PM. File vật lý nằm trong kho artifact của Control Node: `control/data/artifacts/{run_id}/{artifact_id}-project-report-vN.docx` theo cấu hình lưu trữ mặc định.

Tính năng áp dụng cho các lần hoàn tất managed sau khi cập nhật. Release đã được lưu trước đó không tự được viết lại; API trả `404` nếu release cũ chưa có báo cáo. Luồng Conductor `legacy` không tạo file Word này.

## Kiểm tra

```powershell
$env:PYTHONPATH = 'contract;.'
python -m pytest control/tests/test_project_reports.py control/tests/test_change_requests.py control/tests/test_module_projects.py -q
```

Kiểm tra Word thật bằng `python-docx`, nội dung tiếng Việt, kế thừa module/tài liệu qua phiên bản, giữ nguyên file cũ, lỗi ghi file/checksum chặn phát hành, retry không tạo thêm báo cáo và API tải có xác thực. Các bài kiểm tra dùng SQLite, tệp thật và FakeWorkTracker; chưa xác minh trên cụm triển khai hoặc Linear thật.
