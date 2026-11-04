# THIẾT KẾ KIẾN TRÚC HỆ THỐNG (SYSTEM ARCHITECTURE)

**Người lập:** System Architect AI Agent (`sa-01` - PC3: 26.53.120.228:8004)  
**Mô hình kiến trúc:** 3-Tier Layered Architecture with Microservices Readiness  

## 1. Sơ đồ Kiến trúc Tổng thể
```
[Client / Frontend: React / Vite]
         │  HTTPS / REST JSON
         ▼
[API Gateway / Reverse Proxy: Nginx]
         │  Port 80/443 -> 8000
         ▼
[Application Service: FastAPI (Python 3.14)]
   ├── Routers: /auth, /tasks, /categories
   ├── Middlewares: CORS, RateLimit, JWT Auth
   └── ORM Layer: SQLAlchemy Core / Pydantic v2
         │
         ▼
[Database Layer: PostgreSQL 16 / SQLite Persistent]
```

## 2. Thành phần Công nghệ Đề xuất
- **Backend Framework:** FastAPI (Python 3.12+) với Uvicorn ASGI Server.
- **Database:** PostgreSQL (Production) / SQLite (Development/Test).
- **Authentication:** OAuth2 Password Bearer flow với JWT (HS256 hoặc RS256).
- **Containerization:** Docker đa tầng (Multi-stage build) và Docker Compose.
