# Đầu vào và sản phẩm của từng agent

Bảng theo luồng managed hiện tại, gồm xây dựng mới và chỉnh sửa sau bàn giao. Đầu vào áp dụng theo từng bước công việc, không phải mọi tài liệu trong một ô đều cần đồng thời. SRS là tài liệu đặc tả yêu cầu phần mềm. Tên tệp là ví dụ; hệ thống kiểm tra loại artifact và task nguồn, không bắt buộc đúng các tên này.

| Agent | Đầu vào cần có | Sản phẩm phải tạo |
|---|---|---|
| **PM — Quản lý dự án** | Yêu cầu người dùng, phạm vi/ràng buộc. Khi lập thay đổi: tài liệu và thông tin phiên bản đã nghiệm thu. Khi báo cáo: trạng thái task và tài liệu bàn giao của các agent. | Kế hoạch module/task (`module-plan.json`, loại `PLAN`); kế hoạch được Control Node lưu vào DB/Linear. Báo cáo tiến độ (`status-report.md`) và báo cáo tổng kết toàn dự án (`project-report-vN.docx`, do Control Node tổng hợp ở bước hoàn tất), loại `DOCUMENTATION`. |
| **PO — Chủ sản phẩm** | Tạo backlog: mô tả yêu cầu. Cập nhật backlog: thêm backlog và SRS cũ. Nghiệm thu: báo cáo kiểm thử và thông tin triển khai. | Product backlog (`backlog.json`, `BACKLOG`); tài liệu nghiệm thu bản phát hành (`acceptance.md`, `DOCUMENTATION`). |
| **BA — Phân tích nghiệp vụ** | Viết SRS chung: backlog. Viết SRS module: backlog và SRS chung. Tách user stories: SRS. Khi sửa: thêm SRS/user stories cũ tương ứng. | SRS toàn dự án và SRS từng module (`srs.md`, `srs-module.md`, `REQUIREMENT`); user stories có tiêu chí nghiệm thu (`user-stories.json`, `USER_STORY`). |
| **SA — Kiến trúc hệ thống** | SRS và user stories; nếu sửa kiến trúc thì thêm kiến trúc đã bàn giao. | Thiết kế kiến trúc, thành phần, giao tiếp và lựa chọn công nghệ (`architecture.md`, `ARCHITECTURE`). |
| **DBA — Cơ sở dữ liệu** | Kiến trúc và SRS; nếu thay đổi CSDL thì thêm schema cũ. | Lược đồ CSDL, bảng/khóa/chỉ mục và SQL (`schema.sql`, `DATABASE_SCHEMA`). Khi sửa CSDL, nhiệm vụ còn yêu cầu migration bảo toàn dữ liệu và phương án rollback. |
| **DEV — Lập trình** | Khởi tạo: kiến trúc, schema và bộ test. Thực hiện task: thêm SRS, user stories và source gần nhất. Sửa lỗi: source bị lỗi và báo cáo test/bảo mật tương ứng. | Toàn bộ source tích hợp các task đã làm (`source.zip`, `SOURCE_CODE`), gồm phần sửa/tính năng mới; không chỉ trả về phần code thay đổi. |
| **TESTER — Kiểm thử** | Tạo bộ test: user stories, kiến trúc, schema; test module dùng thêm SRS. Chạy test: đúng source của task, bộ test và SRS tương ứng. Hồi quy khi chỉnh sửa: thêm bộ test kế thừa của các module không sửa. | Bộ test case/kịch bản kiểm thử (`test-cases.json`, `TEST_CASE`); báo cáo PASS/FAIL và lỗi phát hiện (`test-report.json`, `TEST_REPORT`). |
| **SECURITY — Bảo mật** | Source cần rà soát và kiến trúc hệ thống. | Báo cáo bảo mật có verdict, phát hiện và mức độ nghiêm trọng (`security-report.json`, `SECURITY_REPORT`). |
| **DEVOPS — Đóng gói và triển khai** | Tạo CI: source. Build: source và cấu hình CI/Docker. Deploy: thông tin image/đóng gói. Health check: thông tin dịch vụ đã triển khai. Các điều kiện test, security và phê duyệt được bộ điều phối kiểm tra trước các bước liên quan. | Dockerfile, cấu hình CI/CD, thông tin image, báo cáo triển khai và health check. Trong workflow, các bước này đều trả artifact loại `DEPLOYMENT`. |
| **SCRUM_MASTER — Điều phối quy trình** | Review module: source và báo cáo test. Retrospective: tài liệu nghiệm thu. Tiến độ task còn được gửi trong context. | Báo cáo review module và retrospective (`module-review.md`, `retrospective.md`, `DOCUMENTATION`). |

## Cách dùng các tài liệu

- Báo cáo Word cuối dự án được tạo trước khi run hoàn thành và có phiên bản riêng sau mỗi lần chỉnh sửa. Xem cách tải tại [PROJECT_REPORT.md](PROJECT_REPORT.md).
- Khi agent bị nghẽn hoặc thiếu sản phẩm, PM có thể khôi phục file/thử lại và trực tiếp tạo tài liệu hoặc source thay agent qua `recover_task`. Phần làm thay vẫn qua kiểm thử/nghiệm thu; xem [PM_AUTO_RECOVERY.md](PM_AUTO_RECOVERY.md).

- SRS chung được giữ làm yêu cầu xuyên suốt; các task trong một module dùng lại SRS module đó.
- Source của DEV là bản tích hợp đầy đủ. Tester nhận đúng artifact của task DEV cần kiểm tra, không tự lấy một bản source bất kỳ.
- Khi sửa dự án đã hoàn thành, tài liệu/source cũ lấy từ release được cố định bằng ID/checksum. Chỉ gọi SA/DBA khi kế hoạch có ảnh hưởng đến kiến trúc/CSDL. Bộ test chung và test của module bị ảnh hưởng được cập nhật; test của các module không sửa được kế thừa cho hồi quy.
- Kế hoạch thực thi, nguồn artifact, trạng thái Linear và manifest phiên bản do PM/Control Node quản lý; Worker thực hiện việc upload/download sản phẩm. Manifest release hiện lưu trong database, không phải một agent thứ 11.

Nguồn đối chiếu: `process/software_delivery.yaml`, `contract/agent_contract/project_plan.py`, `control/app/services/plan_service.py` và các handler của PM. Danh sách `input_artifact_types`, `input_artifact_sources` và `baseline_artifact_ids` trên task cụ thể quyết định chính xác bộ đầu vào cho lần thực hiện đó.
