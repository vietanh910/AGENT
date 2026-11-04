# =============================================================================
# MÃ NGUỒN BACKEND CHẠY ĐƯỢC - HE THONG QUAN LY THU VIEN SACH TRUC TUYEN
# Tự động sinh bởi DEV Agent (PC2) & Multi-Agent System
# =============================================================================

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime

app = FastAPI(
    title="He Thong Quan Ly Thu Vien Sach Truc Tuyen API",
    description="Dịch vụ API quản lý sản phẩm sinh bởi Multi-Agent System: He Thong Quan Ly Thu Vien Sach Truc Tuyen",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class ItemModel(BaseModel):
    id: Optional[str] = None
    title: str = Field(..., min_length=2, max_length=150)
    category: Optional[str] = "Chung"
    description: Optional[str] = ""
    status: Optional[str] = "Active"
    created_at: Optional[str] = None

# In-memory storage with initial sample data
DB: List[dict] = [
    {
        "id": "1",
        "title": "Thiết lập môi trường dự án",
        "category": "Kỹ thuật",
        "description": "Cấu hình Docker và biến môi trường",
        "status": "Hoàn thành",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    },
    {
        "id": "2",
        "title": "Triển khai xác thực và phân quyền",
        "category": "Bảo mật",
        "description": "Tích hợp JWT Bearer Token",
        "status": "Đang thực hiện",
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
]

@app.get("/api/items", response_model=List[dict])
def list_items():
    """Lấy danh sách toàn bộ bản ghi dữ liệu."""
    return DB

@app.post("/api/items", status_code=201)
def create_item(item: ItemModel):
    """Tạo mới một mục dữ liệu."""
    new_item = item.model_dump()
    new_item["id"] = str(uuid.uuid4())[:8]
    new_item["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    DB.insert(0, new_item)
    return new_item

@app.delete("/api/items/{item_id}")
def delete_item(item_id: str):
    """Xóa một mục dữ liệu theo ID."""
    global DB
    original_len = len(DB)
    DB = [i for i in DB if i["id"] != item_id]
    if len(DB) == original_len:
        raise HTTPException(status_code=404, detail="Không tìm thấy mục cần xóa")
    return {"success": True, "deleted_id": item_id}

if __name__ == "__main__":
    import uvicorn
    print("=" * 70)
    print(" KHỞI ĐỘNG MÁY CHỦ HE THONG QUAN LY THU VIEN SACH TRUC TUYEN")
    print(" Địa chỉ truy cập UI / API: http://localhost:8080")
    print(" Tài liệu Swagger API:       http://localhost:8080/docs")
    print("=" * 70)
    uvicorn.run(app, host="0.0.0.0", port=8080)
