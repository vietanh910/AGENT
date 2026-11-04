# TÀI LIỆU ĐẶC TẢ YÊU CẦU PHẦN MỀM (SRS)

**Người lập:** Business Analyst AI Agent (`ba-01` - PC4: 26.222.181.101:8003)  
**Phiên bản:** 1.0.0  
**Tài liệu tham chiếu:** IEEE 830-1998  

## 1. Yêu cầu Chức năng (Functional Requirements)
- **FR-01: Quản lý Phiên Người Dùng:**
  - `POST /api/v1/auth/register`: Đăng ký tài khoản (email, password, full_name).
  - `POST /api/v1/auth/login`: Xác thực và trả về Bearer Token.
- **FR-02: Quản lý Công Việc (Task CRUD):**
  - `POST /api/v1/tasks`: Tạo mới task.
  - `GET /api/v1/tasks`: Lấy danh sách task kèm phân trang (`page`, `limit`).
  - `GET /api/v1/tasks/{id}`: Xem chi tiết task.
  - `PUT /api/v1/tasks/{id}`: Chỉnh sửa thông tin task.
  - `DELETE /api/v1/tasks/{id}`: Xóa mềm (soft delete) task.
- **FR-03: Quản lý Nhãn & Danh mục:**
  - `POST /api/v1/categories`: Tạo danh mục mới.
  - `GET /api/v1/categories`: Liệt kê các danh mục của người dùng.

## 2. Yêu cầu Phi chức năng (Non-Functional Requirements)
- **NFR-01 (Hiệu năng):** Thời gian phản hồi 95% request API dưới 200ms với 500 CCU.
- **NFR-02 (Bảo mật):** Toàn bộ API được bảo vệ qua JWT Token. Mật khẩu lưu trữ với Argon2/Bcrypt salt=12.
- **NFR-03 (Tính toàn vẹn dữ liệu):** Sử dụng khóa ngoại ràng buộc quan hệ `user_id` và `category_id`.
