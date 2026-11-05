# Project Manager (PM) Agent (`pm-01`)

**Luồng dự án mặc định hiện dùng Control Node:** lập backlog theo module/task trên Linear trước khi chạy, bàn giao SRS/code theo task nguồn và kiểm thử sau từng task. Xem [hướng dẫn cấu hình, API và CLI](../../docs/MODULE_PROJECTS.md). Phần PM Conductor bên dưới mô tả chế độ `PM_PROJECT_MODE=legacy` để tiếp tục dự án cũ.

**Chỉnh sửa dự án đã hoàn thành:** `python cli.py project change <project_id> --request "Nội dung cần sửa"`. Xem [yêu cầu thay đổi và lịch sử phiên bản](../../docs/CHANGE_REQUESTS.md); cần chạy migration `002_project_releases` trước khi dùng.

Agent chịu trách nhiệm quản lý dự án, phân tích yêu cầu sơ bộ, lập kế hoạch Sprint/Milestone, phân rã và phân công công việc, theo dõi tiến độ và điều phối tự động quy trình phát triển phần mềm qua PM Conductor.

---

## 1. Danh sách Action hỗ trợ (Contract v1.1.0)

| Action | Capability | Artifact đầu ra | Mô tả |
|---|---|---|---|
| `analyze_requirement` | `project_planning` | `requirements-summary.md` (`REQUIREMENT`) | Phân tích yêu cầu sơ bộ từ brief |
| `create_plan` | `project_planning` | `plan.md` (`PLAN`) | Lập kế hoạch dự án, sprint, milestone |
| `decompose_tasks` | `task_decomposition` | `tasks.json` (`BACKLOG`) | Phân rã công việc kỹ thuật chi tiết |
| `assign_tasks` | `task_assignment` | `tasks.json` (`BACKLOG`) | Phân công task cho DEV, TESTER |
| `track_progress` | `progress_tracking` | Cập nhật `tasks.json` | Theo dõi tỷ lệ hoàn thành tác vụ |
| `report_status` | `progress_tracking` | `status-report.md` (`DOCUMENTATION`) | Báo cáo trạng thái 4 phần chuẩn; khi managed hoàn tất, Control Node tổng hợp thêm `project-report-vN.docx` vào sản phẩm của task PM |
| `recover_task` | `workflow_orchestration` | Artifact theo loại công việc gốc | PM trực tiếp tạo tài liệu/source khi agent bị nghẽn; source được ghép tích lũy, giữ các bước kiểm thử/nghiệm thu |
| `start_project` | `workflow_orchestration` | `ProjectState` | Khởi chạy quy trình phát triển phần mềm tự động |
| `resume_project` | `workflow_orchestration` | `ProjectState` | Tiếp tục dự án từ bước dở dang |
| `get_project_state` | `workflow_orchestration` | `ProjectState` | Truy vấn trạng thái chi tiết của dự án |
| `dispatch_task` | `task_dispatch` | `TaskResult` | Giao tác vụ trực tiếp tới agent khác qua LAN |

---

## 2. Chế độ tự động (PM Conductor Workflow Orchestration)

PM Conductor điều phối tự động toàn bộ vòng đời phát triển phần mềm qua mạng LAN (16 bước tiêu chuẩn từ brief tới bản phát hành RELEASE) theo workflow định nghĩa tại `conductor/workflows/software_delivery.yaml`.

### Điều khiển qua CLI

```bash
# 1. Khởi động dự án mới từ file brief
python -m cli project start --brief-file examples/brief.md

# Tùy chọn tự động duyệt qua các cổng kiểm soát (auto-approve):
python -m cli project start --brief-file examples/brief.md --auto-approve

# Tùy chọn cho phép triển khai thật (deploy production sau khi duyệt cổng):
python -m cli project start --brief-file examples/brief.md --auto-approve --allow-real-deploy

# 2. Xem trạng thái dự án dạng bảng trực quan (kèm theo dõi thời gian thực)
python -m cli project status PROJ-1730600000 --watch

# 3. Phê duyệt thủ công cổng kiểm soát (GATE)
python -m cli project approve PROJ-1730600000 --gate approve_scope
python -m cli project approve PROJ-1730600000 --gate approve_deploy

# 4. Khôi phục dự án sau khi tiến trình PM bị ngắt quãng
python -m cli project resume PROJ-1730600000

# 5. Hủy bỏ dự án đang chạy
python -m cli project abort PROJ-1730600000

# 6. Liệt kê danh sách tất cả các dự án đã tạo
python -m cli project list
```

### Điều khiển qua REST API (Agent API v1 mở rộng)

Mọi endpoint quản lý dự án yêu cầu header `Authorization: Bearer <AGENT_API_KEY>`.

- `POST /projects`: Khởi tạo và chạy ngầm dự án (`{brief, auto_approve, allow_real_deploy}`) -> trả ngay `{project_id, status: CREATED}`.
- `GET /projects`: Danh sách `{project_id, status, current_step, created_at}`.
- `GET /projects/{project_id}`: Chi tiết `ProjectState` đầy đủ (các bước, artifact, lịch sử).
- `POST /projects/{project_id}/approve`: Duyệt cổng (`{gate: "approve_scope"|"approve_deploy"}`) và tiếp tục chạy ngầm.
- `POST /projects/{project_id}/resume`: Tiếp tục chạy ngầm bước dở dang.
- `POST /projects/{project_id}/abort`: Chuyển trạng thái sang `ABORTED` và dừng bước tiếp theo.

---

## 3. Cấu hình Biến Môi trường (.env)

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `AGENT_ID` | `pm-01` | Mã định danh PM |
| `AGENT_ROLE` | `PM` | Vai trò Agent |
| `AGENT_API_KEY` | *(bắt buộc khi HOST!=127.0.0.1)* | Bearer token xác thực API |
| `MAX_UPLOAD_MB` | `50` | Giới hạn upload file artifact qua inbox |
| `PM_AUTO_APPROVE` | `false` | Tự động phê duyệt các cổng kiểm soát |
| `PM_ALLOW_REAL_DEPLOY` | `false` | Cho phép deploy thật (`context.execute=true`) sau khi duyệt cổng |
| `MAX_FIX_LOOPS` | `3` | Số vòng sửa lỗi tối đa khi TESTER/SECURITY phát hiện lỗi |
| `MAX_TASKS_PER_PROJECT` | `60` | Giới hạn tối đa số task được giao cho một dự án |
| `PROJECT_TIMEOUT_MINUTES` | `240` | Thời gian chạy tối đa của một dự án trước khi dừng an toàn |

---

## 4. Cách chạy Agent độc lập & Kiểm thử

```bash
# Chạy demo offline
python demo.py --mock

# Chạy FastAPI service
uvicorn api:app --reload --port 8001

# Chạy toàn bộ Unit Tests của PM
python -m pytest tests/ -v

# Chạy bộ 13 Integration Tests của Conductor
python -m pytest ../../tests/test_conductor.py -v
```
