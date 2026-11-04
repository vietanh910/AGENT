# PRODUCT BACKLOG & USER STORIES

**Người lập:** Product Owner AI Agent (`po-01` - PC3: 26.53.120.228:8005)  
**Tầm nhìn:** Ứng dụng quản lý việc cá nhân tối giản, trực quan và bảo mật.

## Danh sách User Stories (Theo độ ưu tiên MoSCoW)

### Must-Have (Bắt buộc)
1. **US-01: Đăng ký & Đăng nhập tài khoản**
   - *Là người dùng mới*, tôi muốn đăng ký tài khoản với email/mật khẩu để lưu trữ dữ liệu riêng tư.
   - *Tiêu chuẩn nghiệm thu:* Mã hóa mật khẩu bcrypt, cấp JWT access token hạn 24h.
2. **US-02: Thêm mới công việc (Create Task)**
   - *Là người dùng*, tôi muốn tạo một công việc mới với tiêu đề, mô tả và ngày hết hạn.
   - *Tiêu chuẩn nghiệm thu:* Tiêu đề từ 3-255 ký tự, ngày hết hạn không nhỏ hơn ngày hiện tại.
3. **US-03: Cập nhật trạng thái công việc (Update Status)**
   - *Là người dùng*, tôi muốn đánh dấu hoàn thành một việc chỉ với 1 click.

### Should-Have (Nên có)
4. **US-04: Phân loại theo Danh mục và Nhãn**
   - *Là người dùng*, tôi muốn gắn thẻ tag (e.g., #urgent, #work, #personal) để dễ tìm kiếm.
5. **US-05: Tìm kiếm và Lọc đa tiêu chí**
   - *Là người dùng*, tôi muốn lọc danh sách việc theo hạn chót, danh mục và trạng thái.

### Could-Have (Có thể mở rộng)
6. **US-06: Thống kê hiệu suất tuần (Productivity Dashboard)**
   - Biểu đồ thống kê số lượng task hoàn thành theo ngày.
