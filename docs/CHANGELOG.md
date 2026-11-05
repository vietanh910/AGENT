# CHANGELOG

Tất cả các thay đổi về Agent Contract sẽ được ghi lại tại tài liệu này.

## [1.4.0] - 2026-11-05
- Thêm action PM `recover_task` dùng capability `workflow_orchestration` hiện có, cho phép tạo tài liệu/code thay agent gặp lỗi theo yêu cầu người dùng.
- Dùng trường metadata hiện có để gắn `dispatch_token` vào kết quả Worker, tránh nhận nhầm callback của lượt thực hiện cũ sau khôi phục.
- Không thêm trường bắt buộc vào TaskRequest/TaskResult; PM không tự cấp verdict kiểm thử/bảo mật, nghiệm thu hoặc triển khai thật qua action phục hồi.

## [1.3.0] - 2026-11-04
### Bổ sung Metadata cho ArtifactRef (Prompt S6 - Artifact Store & GitHub)
- Nâng `CONTRACT_VERSION` từ `"1.2.0"` lên `"1.3.0"`. `CONTRACT_MIN_VERSION` của các agent giữ nguyên `"1.0.0"`.
- Bổ sung trường `metadata: dict[str, Any] = Field(default_factory=dict)` vào `ArtifactRef` để lưu trữ thông tin commit sha, PR number, branch, verdict, v.v. (tương thích ngược hoàn toàn với phiên bản cũ).
- Cập nhật 17 JSON Schemas trong `contract/schemas/`.

## [1.2.0] - 2026-11-04
### Mở rộng Contract cho Hệ thống (Prompt S2 - System Phase)
- Nâng `CONTRACT_VERSION` từ `"1.1.0"` lên `"1.2.0"`. `CONTRACT_MIN_VERSION` của các agent giữ nguyên `"1.0.0"`.
- Bổ sung `system_models.py` phục vụ giai đoạn System Phase (PC1 Control Node & Worker Hosts PC2..PC5):
  - Models: `WorkerRegistration`, `HeartbeatAgentStatus`, `Heartbeat`, `DispatchRequest`, `DispatchAck`, `WorkflowRun`, `RunTask`, `Approval`, `AuditEvent`.
  - Enums: `RunStatus`, `DispatchState`, `SystemEventType`.
  - Bảng ánh xạ trạng thái `DISPATCH_TO_TASK_STATUS` (từ `DispatchState` trong DB sang `TaskStatus` của Contract).
- Bổ sung 5 giá trị mã lỗi `ErrorCode`: `WORKER_OFFLINE`, `DISPATCH_FAILED`, `LINEAR_ERROR`, `GITHUB_ERROR`, `APPROVAL_REQUIRED`.
- Bổ sung Model `Verdict` (gồm `verdict`: `PASS` | `FAIL`, `findings`: list[{severity, title, detail}]) bắt buộc cho `outputs[0]` của TESTER, SECURITY và PO (`accept_release`).
- Mở rộng `ArtifactRef.uri`: Cho phép thêm 3 dạng URI scheme: `github://owner/repo/pull/<n>`, `github://owner/repo/commit/<sha>`, `artifact://<id>`. Tiếp tục chặn đường dẫn tuyệt đối và path traversal `..`.
- Bổ sung danh bạ `AGENT_REGISTRY_DEFAULT` gồm 10 Agent theo đúng bảng phân bổ trên 5 máy (PC1: PM, DevOps; PC2: SA, DBA; PC3: BA, PO; PC4: DEV, TESTER; PC5: SCRUM_MASTER, SECURITY).
- Chuẩn hóa validation `agent_id` theo định dạng `<role viết thường>-NN` với role thuộc `AgentRole`.
- Đăng ký bổ sung pipeline actions: DEV `implement_feature`, SCRUM_MASTER `run_sprint_review` vào `ACTION_MAP` và `CAPABILITY_REGISTRY`.
- Cập nhật công cụ `export_schemas` xuất 17 JSON Schemas hoàn chỉnh.

## [1.1.0] - 2026-11-03
### Nâng cấp Contract v1.1.0 (Prompt 8 - PM Conductor)
- Nâng `CONTRACT_VERSION` từ `"1.0.0"` lên `"1.1.0"`. `CONTRACT_MIN_VERSION` của các agent giữ nguyên `"1.0.0"`.
- Bổ sung capabilities cho PM: `task_dispatch`, `workflow_orchestration`.
- Bổ sung actions cho PM: `start_project`, `resume_project`, `get_project_state` (workflow_orchestration) và `dispatch_task` (task_dispatch).
- Đăng ký toàn bộ pipeline actions ở Bảng 2.9 vào `ACTION_MAP` và `CAPABILITY_REGISTRY` cho toàn bộ 10 Agent roles.
- Bổ sung mục 2.9 "Pipeline actions" vào `docs/CONTRACT.md`:
  - Quy ước verdict: `outputs[0]` của TESTER, SECURITY và PO có cấu trúc `{"verdict": "PASS" | "FAIL", "findings": [...]}`.
  - Quy ước `SOURCE_CODE`: chuyển giao qua file nén `.zip`.
- Tăng giới hạn `MAX_UPLOAD_MB` mặc định lên 50MB.
- Ngoại lệ điều phối: Cho phép duy nhất PM import module `conductor` và gọi HTTP tới các agent khác. Các agent khác vẫn bị nghiêm cấm giao tiếp ngang hàng.

## [1.0.0] - 2026-11-03
### Bổ sung Agent API v1 (Prompt 7)
- Thêm mục 2.8 "Agent API v1" vào `docs/CONTRACT.md`:
  - Chuẩn hóa các endpoint HTTP: `/health`, `/agent`, `/state`, `/tasks`, `/tasks/{task_id}`, `/artifacts`, `/artifacts/{id}/content`, `/artifacts/inbox`.
  - Cơ chế xác thực Bearer Token qua header `Authorization: Bearer <AGENT_API_KEY>` (ngoại trừ `/health`).
  - Thêm `AGENT_API_KEY`, `MAX_UPLOAD_MB` vào cấu hình `.env.example`.
  - Không đổi schema Pydantic nên không tăng `CONTRACT_VERSION` (vẫn giữ 1.0.0).

## [1.0.0] - 2026-11-02
### Cập nhật cấu hình môi trường
- thêm HOST, PORT, CONTRACT_MIN_VERSION; không đổi schema nên không tăng CONTRACT_VERSION.

### Khởi tạo ban đầu
- Định nghĩa các Enum: `AgentRole`, `AgentStatus`, `TaskStatus`, `Priority`, `ArtifactType`, `ErrorCode`.
- Định nghĩa các Model chuẩn (Pydantic v2): `AgentIdentity`, `AgentState`, `ArtifactRef`, `Constraints`, `TaskRequest`, `AgentError`, `NextAction`, `TaskResult`.
- Thiết lập `CAPABILITY_REGISTRY` cho 10 roles và `ACTION_MAP` cho PM & DevOps (8 roles còn lại để trống chờ bổ sung).
- Ràng buộc validator:
  - `status == COMPLETED` -> `error` bắt buộc là `None`.
  - `status in (FAILED, BLOCKED)` -> `error` bắt buộc phải có.
  - `status == CANCELLED` -> `error` tùy chọn (`None` hoặc `AgentError`).
  - `uri` artifact là tương đối và không chứa `..`.
  - `agent_id` regex `^[a-z_]+-\d{2}$`.
  - `capabilities` là tập con của `CAPABILITY_REGISTRY[role]`.
- Cung cấp các hàm tiện ích: `make_error`, `failed_result`, `blocked_result`.
- Hỗ trợ công cụ xuất schema `python -m agent_contract.export_schemas`.
