# Kế hoạch dự án: He Thong Quan Ly Thu Vien Sach Truc Tuyen

## 1. Tổng quan
Dự án xây dựng Hệ thống Quản lý Thư viện Sách Trực tuyến nhằm số hóa quy trình quản lý danh mục sách, quản lý độc giả, mượn/trả sách, tìm kiếm và báo cáo thống kê, nâng cao hiệu quả vận hành thư viện.

## 2. Các cột mốc quan trọng (Milestones)
- **MS-01: Khởi động dự án & Phân tích yêu cầu** (Hạn: 2025-04-15)
  - Mô tả: Hoàn tất việc thu thập yêu cầu nghiệp vụ, thiết kế kiến trúc tổng thể và cơ sở dữ liệu.
  - Sản phẩm bàn giao: Tài liệu Đặc tả Yêu cầu Phân tích (SRS), Tài liệu Thiết kế Kiến trúc (SAD), Thiết kế Cơ sở dữ liệu (ERD)
- **MS-02: Hoàn thành phát triển tính năng cốt lõi** (Hạn: 2025-05-30)
  - Mô tả: Xây dựng xong các chức năng chính bao gồm quản lý người dùng, danh mục sách, mượn/trả sách.
  - Sản phẩm bàn giao: Module Quản lý Độc giả & Phân quyền, Module Quản lý Sách & Tìm kiếm, Module Quản lý Mượn/Trả sách
- **MS-03: Kiểm thử hệ thống & Nghiệm thu (UAT)** (Hạn: 2025-06-20)
  - Mô tả: Hoàn thành kiểm thử chức năng, hiệu năng, bảo mật và tổ chức nghiệm thu với đại diện người dùng.
  - Sản phẩm bàn giao: Báo cáo Kiểm thử Tổng thể (Test Report), Biên bản Nghiệm thu UAT
- **MS-04: Triển khai chính thức (Go-live)** (Hạn: 2025-07-05)
  - Mô tả: Triển khai hệ thống lên môi trường Production, bàn giao tài liệu và đào tạo sử dụng.
  - Sản phẩm bàn giao: Hệ thống vận hành chính thức, Tài liệu Hướng dẫn Sử dụng & Bàn giao

## 3. Kế hoạch phân chia Sprint
- **Sprint 1** (2 tuần): Phân tích yêu cầu chi tiết, thiết kế kiến trúc hệ thống và cơ sở dữ liệu
  - Mục tiêu bàn giao: User Stories và Backlog dự án, Sơ đồ kiến trúc microservices/monolith, Script khởi tạo CSDL ban đầu
- **Sprint 2** (2 tuần): Phát triển tính năng xác thực, quản lý tài khoản độc giả và thủ thư
  - Mục tiêu bàn giao: API & UI Đăng ký/Đăng nhập/Đổi mật khẩu, Chức năng Phân quyền người dùng (Role-based Access Control)
- **Sprint 3** (2 tuần): Phát triển quản lý danh mục sách, thể loại, tác giả và tìm kiếm
  - Mục tiêu bàn giao: API & UI CRUD Sách/Tác giả/Thể loại, Chức năng tìm kiếm sách nâng cao theo từ khóa, thể loại
- **Sprint 4** (2 tuần): Phát triển nghiệp vụ mượn sách, trả sách, tính phí phạt quá hạn
  - Mục tiêu bàn giao: Quy trình Đặt mượn - Xác nhận mượn - Trả sách, Tính năng tự động tính quá hạn và gửi thông báo nhắc nhở
- **Sprint 5** (2 tuần): Phát triển báo cáo thống kê và hoàn thiện kiểm thử hệ thống
  - Mục tiêu bàn giao: Dashboard báo cáo số lượng mượn/trả, sách được mượn nhiều nhất, Sửa các lỗi từ đợt kiểm thử QA/QC
- **Sprint 6** (2 tuần): Nghiệm thu UAT, triển khai hạ tầng Production và chính thức Go-live
  - Mục tiêu bàn giao: Hạ tầng CI/CD & Production hoàn thiện, Biên bản Go-live thành công

## 4. Quản lý rủi ro dự án
- [!] Thay đổi yêu cầu nghiệp vụ mượn/trả sách trong quá trình phát triển gây ảnh hưởng tiến độ.
- [!] Nguy cơ nghẽn hệ thống hoặc truy vấn chậm khi số lượng dữ liệu sách và truy cập tăng cao.
- [!] Rủi ro rò rỉ thông tin cá nhân của độc giả nếu không đảm bảo các tiêu chuẩn bảo mật CSDL.
- [!] Thiếu sự phối hợp chặt chẽ từ phía người dùng cuối trong giai đoạn thử nghiệm UAT.
