"""Tạo khung thư mục control/ và worker/ cho PC1 và PC2..PC5."""

from pathlib import Path

FILES = {
    "control/app/__init__.py": '"""Gói ứng dụng Control Node cho PC1 trong hệ thống ai-team."""\n',
    "control/app/api/__init__.py": '"""Tầng định tuyến HTTP API (FastAPI) của Control Node (runs, tasks, workers, agents, artifacts, webhooks)."""\n',
    "control/app/api/runs.py": '"""API endpoints cho quản lý WorkflowRun: tạo run, xem tiến độ, phê duyệt, hủy, tạm dừng."""\n',
    "control/app/api/tasks.py": '"""API endpoints cho quản lý RunTask: truy vấn trạng thái task, callback kết quả từ worker (POST /api/v1/tasks/{id}/result), unblock."""\n',
    "control/app/api/workers.py": '"""API endpoints cho Worker Host: đăng ký worker (/register) và nhận heartbeat định kỳ (/heartbeat)."""\n',
    "control/app/api/agents.py": '"""API endpoints cung cấp danh sách và trạng thái hiện tại của 10 Agent trong hệ thống."""\n',
    "control/app/api/artifacts.py": '"""API endpoints cho Artifact Store tập trung trên PC1: tải lên, tải xuống và xác thực checksum sha256."""\n',
    "control/app/api/webhooks.py": '"""API endpoints tiếp nhận webhook từ Linear và GitHub (tùy chọn, có kiểm tra chữ ký HMAC)."""\n',
    "control/app/orchestrator/__init__.py": '"""Module điều phối quy trình (Orchestrator) sử dụng LangGraph với trạng thái bền vững trong MySQL."""\n',
    "control/app/orchestrator/graph.py": '"""Xây dựng đồ thị trạng thái LangGraph đại diện cho vòng đời phần mềm (software delivery workflow)."""\n',
    "control/app/orchestrator/nodes.py": '"""Các node xử lý trong đồ thị LangGraph tương ứng với từng giai đoạn và vai trò agent."""\n',
    "control/app/orchestrator/edges.py": '"""Các cạnh chuyển tiếp và điều kiện rẽ nhánh (conditional edges) trong đồ thị LangGraph."""\n',
    "control/app/orchestrator/state.py": '"""Định nghĩa cấu trúc trạng thái WorkflowState được truyền qua lại giữa các node trong LangGraph."""\n',
    "control/app/orchestrator/scheduler.py": '"""Vòng lập lịch duyệt DAG: tính toán các task READY theo phụ thuộc và đưa vào hàng đợi điều phối."""\n',
    "control/app/orchestrator/guards.py": '"""Các cổng kiểm tra (guards) và quy tắc tất định: giới hạn số vòng lặp sửa lỗi, timeout, kiểm tra gate."""\n',
    "control/app/services/__init__.py": '"""Tầng nghiệp vụ (Services) của Control Node."""\n',
    "control/app/services/registry.py": '"""Quản lý danh bạ 10 agent (agent_id, role, machine, runner) và ánh xạ worker."""\n',
    "control/app/services/task_manager.py": '"""Quản lý vòng đời task: chuyển trạng thái dispatch_state, cập nhật kết quả, retry với backoff."""\n',
    "control/app/services/dispatcher.py": '"""Điều phối giao việc bất đồng bộ tới LocalWorker (PM, DevOps) và RemoteWorker (PC2-PC5) qua WorkerClient."""\n',
    "control/app/services/approval.py": '"""Xử lý phê duyệt tại các cổng duyệt (gate): APPROVE_SCOPE, APPROVE_PROD qua token ADMIN hoặc Linear."""\n',
    "control/app/services/plan_service.py": '"""Dịch vụ lập kế hoạch dự án: gọi PM Agent sinh kế hoạch, kiểm tra qua plan_validator, fallback kế hoạch mẫu."""\n',
    "control/app/integrations/__init__.py": '"""Tích hợp hệ thống bên ngoài (Linear, GitHub)."""\n',
    "control/app/integrations/linear/__init__.py": '"""Module tích hợp Linear: đồng bộ phản chiếu Project, Milestone, Issue và Sub-Issue."""\n',
    "control/app/integrations/linear/client.py": '"""Client giao tiếp GraphQL với Linear API kèm rate limiting, retry có giới hạn và timeout."""\n',
    "control/app/integrations/linear/sync.py": '"""Xử lý đồng bộ hai chiều hoặc một chiều (polling queue) giữa MySQL và Linear."""\n',
    "control/app/integrations/github/__init__.py": '"""Module tích hợp GitHub: quản lý repository, pull request, commit và artifact code."""\n',
    "control/app/integrations/github/client.py": '"""Client REST/GraphQL với GitHub API: tạo PR, kiểm tra CI status, merge PR."""\n',
    "control/app/database/__init__.py": '"""Tầng lưu trữ dữ liệu bền vững (MySQL 8.4 LTS) sử dụng SQLAlchemy 2.x."""\n',
    "control/app/database/models.py": '"""Khai báo SQLAlchemy ORM models: workers, agents, workflow_runs, tasks, task_dependencies, task_attempts, artifacts, approvals, audit_logs, agent_events, linear_links."""\n',
    "control/app/database/repositories.py": '"""Toàn bộ truy vấn DB tập trung, xử lý concurrency với SELECT ... FOR UPDATE SKIP LOCKED hoặc version optimistic lock."""\n',
    "control/app/database/session.py": '"""Quản lý kết nối DB Engine, SessionFactory với connection pooling (pool_pre_ping, pool_recycle)."""\n',
    "control/app/queue/__init__.py": '"""Hàng đợi thông điệp, khóa phân tán và luồng sự kiện Redis."""\n',
    "control/app/queue/redis.py": '"""Wrapper Redis: hàng đợi theo agent, khóa phân tán lock:task:{id} có TTL, và fakeredis cho test."""\n',
    "control/app/queue/events.py": '"""Phát và đọc sự kiện hệ thống (Redis Streams: events:run:{run_id} và đồng bộ agent_events)."""\n',
    "control/app/security/__init__.py": '"""Module bảo mật, xác thực và phân quyền."""\n',
    "control/app/security/authentication.py": '"""Kiểm tra Bearer token cho Admin và các Worker secret token."""\n',
    "control/app/security/permissions.py": '"""Kiểm tra quyền hạn thực hiện hành động trên Control Node."""\n',
    "control/app/security/secrets.py": '"""Quản lý và giải mã an toàn các bí mật cấu hình, không bao giờ ghi secret ra log."""\n',
    "control/app/observability/__init__.py": '"""Quan sát hệ thống: logging có cấu trúc và audit log."""\n',
    "control/app/observability/logging.py": '"""Cấu hình logging định dạng JSON có cấu trúc, tự động làm sạch (redact) token và mật khẩu."""\n',
    "control/app/observability/audit.py": '"""Ghi nhận AuditEvent cho mọi cuộc gọi adapter, thao tác phê duyệt và thay đổi trạng thái quan trọng."""\n',
    "control/migrations/__init__.py": '"""Alembic migrations cho cơ sở dữ liệu MySQL."""\n',
    "control/migrations/README.md": '# Database Migrations (Alembic & MySQL 8.4 LTS)\n\nThư mục chứa các tệp version migration quản lý bởi Alembic cho MySQL 8.4 LTS.\n',
    "control/tests/__init__.py": '"""Bộ kiểm thử cho Control Node (PC1)."""\n',
    "worker/host/__init__.py": '"""Worker Host dùng chung cho PC2 đến PC5 và chạy 2 agent trên mỗi máy."""\n',
    "worker/host/main.py": '"""Điểm khởi động chính của Worker Host tiến trình."""\n',
    "worker/host/api.py": '"""FastAPI server của Worker Host (cổng 8000): GET /health, GET /agents, POST /tasks, POST /tasks/{id}/cancel."""\n',
    "worker/host/runner.py": '"""Bộ thực thi chạy agent nội bộ trên Worker Host, cô lập tiến trình và bắt timeout."""\n',
    "worker/host/outbox.py": '"""Hộp thư đi (Outbox) lưu bằng SQLite trên máy worker, bảo đảm kết quả gửi về PC1 không bị mất khi rớt mạng."""\n',
    "worker/host/heartbeat.py": '"""Tiến trình gửi heartbeat định kỳ mỗi 10 giây báo cáo trạng thái agent và task đang chạy về PC1."""\n',
    "worker/host/config.py": '"""Đọc và validate cấu hình Worker Host từ file worker.yaml và biến môi trường."""\n',
    "worker/tests/__init__.py": '"""Bộ kiểm thử cho Worker Host."""\n',
}


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    for rel_path, content in FILES.items():
        dest = root / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            dest.write_text(content, encoding="utf-8")
            print(f"Created: {rel_path}")
        else:
            print(f"Already exists: {rel_path}")


if __name__ == "__main__":
    main()
