# HƯỚNG DẪN CHẠY ỨNG DỤNG (He Thong Quan Ly Thu Vien Sach Truc Tuyen)

Sản phẩm mã nguồn đơn giản đã được sinh hoàn chỉnh và có thể chạy ngay theo 2 cách:

### Cách 1: Chạy trực tiếp Giao diện Web (Không cần cài thêm thư viện)
Mở trực tiếp file `index.html` bằng trình duyệt web:
```powershell
start C:\Users\Admin\Desktop\qldapm-4(5)\AGENT\deliverables\he_thong_quan_ly_thu_vien\src\index.html
```

### Cách 2: Chạy Web Server Backend FastAPI (Python)
Cài đặt thư viện (nếu chưa có):
```powershell
pip install fastapi uvicorn
```
Khởi động server:
```powershell
python C:\Users\Admin\Desktop\qldapm-4(5)\AGENT\deliverables\he_thong_quan_ly_thu_vien\src\app.py
```
Sau đó truy cập:
- Giao diện: **http://localhost:8080**
- API Docs:  **http://localhost:8080/docs**
