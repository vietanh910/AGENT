# Agent Contract v1.0

> Đây là phần cần thống nhất với 4 người còn lại trước tiên. Sau khi **freeze** (Phase 7) chỉ được sửa bằng cách tăng version.

## 2.1 Enum thống nhất

| Enum | Giá trị |
| --- | --- |
| `AgentRole` | `PM`, `SA`, `BA`, `PO`, `DEV`, `TESTER`, `DEVOPS`, `DBA`, `SCRUM_MASTER`, `SECURITY` |
| `AgentStatus` | `OFFLINE`, `IDLE`, `RUNNING`, `WAITING`, `COMPLETED`, `FAILED`, `BLOCKED` |
| `TaskStatus` | `PENDING`, `RUNNING`, `COMPLETED`, `FAILED`, `BLOCKED`, `CANCELLED` |
| `Priority` | `LOW`, `MEDIUM`, `HIGH`, `CRITICAL` |
| `ArtifactType` | `REQUIREMENT`, `USER_STORY`, `ARCHITECTURE`, `DATABASE_SCHEMA`, `SOURCE_CODE`, `TEST_CASE`, `TEST_REPORT`, `SECURITY_REPORT`, `DEPLOYMENT`, `DOCUMENTATION`, `PLAN`, `BACKLOG`, `OTHER` |
| `ErrorCode` | `INVALID_INPUT`, `MISSING_CONTEXT`, `LLM_ERROR`, `TOOL_ERROR`, `TIMEOUT`, `PERMISSION_DENIED`, `ARTIFACT_ERROR`, `VALIDATION_ERROR`, `UNKNOWN_ERROR`, `WORKER_OFFLINE`, `DISPATCH_FAILED`, `LINEAR_ERROR`, `GITHUB_ERROR`, `APPROVAL_REQUIRED` |

Ghi chú: `PLAN`, `BACKLOG`, `OTHER` là phần bổ sung so với danh sách gốc, vì PM và PO cần xuất kế hoạch và backlog.

## 2.2 Các model

```python
CONTRACT_VERSION = "1.3.0"

class AgentIdentity(BaseModel):          # Agent tự khai báo
    contract_version: str = CONTRACT_VERSION
    agent_id: str                         # dạng pm-01, devops-01 (regex ^[a-z_]+-\d{2}$)
    role: AgentRole
    name: str
    version: str                          # version của agent
    capabilities: list[str]               # phải nằm trong CAPABILITY_REGISTRY[role]
    status: AgentStatus = AgentStatus.IDLE

class AgentState(BaseModel):             # Dashboard sau này đọc cái này
    agent_id: str
    role: AgentRole
    status: AgentStatus
    current_task: str | None = None
    updated_at: datetime

class ArtifactRef(BaseModel):
    artifact_id: str                      # ART-001
    name: str
    type: ArtifactType
    version: str = "1.0"
    uri: str                              # đường dẫn tương đối, ví dụ artifacts/requirements.md
    created_by: str                       # agent_id
    created_at: datetime
    checksum: str | None = None           # sha256, để phát hiện file bị sửa
    metadata: dict = {}                   # commit_sha, pr_number, branch, verdict...

class Constraints(BaseModel):
    deadline: datetime | None = None
    priority: Priority = Priority.MEDIUM
    timeout_seconds: int = 300

class TaskRequest(BaseModel):
    contract_version: str = CONTRACT_VERSION
    task_id: str                          # TASK-001
    project_id: str
    agent_id: str
    role: AgentRole
    action: str                           # phải nằm trong danh sách action của role
    instruction: str
    context: dict = {}
    input_artifacts: list[ArtifactRef] = []
    constraints: Constraints = Constraints()

class AgentError(BaseModel):
    code: ErrorCode
    message: str
    retryable: bool
    details: dict = {}

class NextAction(BaseModel):              # gợi ý, KHÔNG phải lệnh gọi agent khác
    suggested_role: AgentRole
    action: str
    reason: str

class TaskResult(BaseModel):
    contract_version: str = CONTRACT_VERSION
    task_id: str
    agent_id: str
    status: TaskStatus                    # kết thúc: COMPLETED | FAILED | BLOCKED | CANCELLED
    summary: str
    outputs: list[dict] = []              # dữ liệu có cấu trúc, schema riêng từng action
    artifacts: list[ArtifactRef] = []
    next_actions: list[NextAction] = []
    error: AgentError | None = None       # bắt buộc có khi FAILED/BLOCKED, null khi COMPLETED
    metadata: dict = {}                   # model, provider, token, duration_ms, dry_run...
```

**Ràng buộc validate (viết bằng `model_validator`)**

- `status == COMPLETED` thì `error` phải là `None`; `FAILED`/`BLOCKED` thì `error` bắt buộc có.
- `TaskRequest.role` phải khớp role của agent nhận.
- `AgentIdentity.capabilities` phải là tập con của `CAPABILITY_REGISTRY[role]`.
- `ArtifactRef.uri` phải là đường dẫn tương đối và không chứa `..`.

## 2.3 Capability Registry và Action Map

Mỗi `action` ánh xạ về đúng một `capability`. Agent nhận action không thuộc capability của mình thì trả `FAILED` + `PERMISSION_DENIED`, `retryable=false`.

| Role | Capabilities |
| --- | --- |
| PM | `project_planning`, `task_decomposition`, `task_assignment`, `progress_tracking`, `task_dispatch`, `workflow_orchestration` |
| BA | `requirement_analysis`, `user_story`, `use_case`, `acceptance_criteria` |
| SA | `system_architecture`, `api_design`, `technology_selection` |
| DBA | `database_design`, `schema_design`, `sql`, `query_optimization` |
| DEV | `coding`, `refactoring`, `bug_fixing`, `code_review` |
| TESTER | `test_planning`, `test_case_generation`, `automated_testing`, `bug_reporting` |
| DEVOPS | `git`, `docker`, `ci_cd`, `deployment`, `monitoring`, `health_check` |
| SECURITY | `security_analysis`, `vulnerability_scan`, `dependency_scan`, `security_review` |
| PO | `product_vision`, `feature_prioritization`, `backlog_management`, `acceptance` |
| SCRUM_MASTER | `sprint_management`, `scrum_management`, `blocker_tracking`, `retrospective` |

**Action của PM**

| Action | Capability |
| --- | --- |
| `analyze_requirement` | `project_planning` |
| `create_plan` | `project_planning` |
| `decompose_tasks` | `task_decomposition` |
| `assign_tasks` | `task_assignment` |
| `track_progress` | `progress_tracking` |
| `report_status` | `progress_tracking` |
| `start_project` | `workflow_orchestration` |
| `resume_project` | `workflow_orchestration` |
| `get_project_state` | `workflow_orchestration` |
| `dispatch_task` | `task_dispatch` |

**Action của DevOps**

| Action | Capability |
| --- | --- |
| `git_operation` | `git` |
| `build_image` | `docker` |
| `generate_ci_config` | `ci_cd` |
| `run_tests` | `ci_cd` |
| `deploy` | `deployment` |
| `health_check` | `health_check` |
| `setup_monitoring` | `monitoring` |

*4 người còn lại tự định nghĩa action cho role của mình theo cùng dạng bảng này, rồi gửi để bạn duyệt và gộp vào registry.*

## 2.4 Ví dụ JSON chuẩn

**TaskRequest**

```json
{
  "contract_version": "1.0.0",
  "task_id": "TASK-001",
  "project_id": "PROJECT-001",
  "agent_id": "pm-01",
  "role": "PM",
  "action": "create_plan",
  "instruction": "Lập kế hoạch 6 sprint cho web bán hàng, 5 người, 3 tháng",
  "context": {"team_size": 5, "duration_weeks": 12},
  "input_artifacts": [],
  "constraints": {"deadline": null, "priority": "HIGH", "timeout_seconds": 300}
}
```

**TaskResult (thành công)**

```json
{
  "contract_version": "1.0.0",
  "task_id": "TASK-001",
  "agent_id": "pm-01",
  "status": "COMPLETED",
  "summary": "Đã tạo kế hoạch 6 sprint gồm 4 milestone",
  "outputs": [{"milestones": 4, "sprints": 6}],
  "artifacts": [{"artifact_id": "ART-001", "name": "plan.md", "type": "PLAN", "version": "1.0", "uri": "artifacts/plan.md", "created_by": "pm-01", "created_at": "2026-11-02T09:00:00Z", "checksum": null}],
  "next_actions": [{"suggested_role": "BA", "action": "analyze_requirement", "reason": "Cần chi tiết hóa yêu cầu cho sprint 1"}],
  "error": null,
  "metadata": {"provider": "groq", "model": "llama-3.3-70b-versatile", "duration_ms": 4200}
}
```

**TaskResult (lỗi)**

```json
{
  "contract_version": "1.0.0",
  "task_id": "TASK-002",
  "agent_id": "devops-01",
  "status": "FAILED",
  "summary": "Build Docker image thất bại",
  "outputs": [], "artifacts": [], "next_actions": [],
  "error": {"code": "TOOL_ERROR", "message": "docker build exit code 1", "retryable": true, "details": {"stderr_tail": "..."}},
  "metadata": {}
}
```

**Quy tắc retry cho Orchestrator sau này**: `FAILED` + `retryable=true` → thử lại; `retryable=false` → `BLOCKED` → chuyển cho người.

## 2.5 Quy ước Artifact

- Ghi file vào thư mục `artifacts/` của agent; mỗi file có một `ArtifactRef` ghi trong `artifacts/index.json`.
- Tên `artifact_id` tăng dần theo từng agent (`ART-001`...); khi ghép hệ thống sẽ thêm tiền tố project.
- Sửa nội dung thì tạo version mới (`1.1`), không ghi đè version cũ.
- Luồng artifact dự kiến: BA `requirements.md` → SA `architecture.md` → DBA `database.sql` → DEV source code → TESTER `test-report.md` → SECURITY `security-report.md` → DEVOPS `deployment-report.md`.

## 2.6 Cấu hình LLM chung (`.env.example`)

```
HOST=0.0.0.0
PORT=                        # bắt buộc, không có giá trị mặc định chung
CONTRACT_MIN_VERSION=1.0.0
AGENT_ID=pm-01
AGENT_ROLE=PM
AGENT_API_KEY=               # bắt buộc nếu HOST != 127.0.0.1
MAX_UPLOAD_MB=50             # giới hạn dung lượng tải file artifact lên (mặc định 50MB)
LLM_PROVIDER=groq            # groq | gemini | openrouter | ollama | openai | custom
LLM_MODEL=llama-3.3-70b-versatile
LLM_API_KEY=
LLM_BASE_URL=                # để trống thì lấy theo preset của provider
LLM_TEMPERATURE=0.2
LLM_TIMEOUT_SECONDS=60
LLM_MAX_RETRIES=2
ARTIFACT_DIR=./artifacts
WORKSPACE_DIR=./workspace    # thư mục làm việc giới hạn cho DevOps
ALLOWED_COMMANDS=git,docker,pytest,npm,mvn,python
DRY_RUN=true
```

## 2.7 Quy tắc cho cả nhóm

1. Mọi agent tuân theo Contract v1.0, I/O dùng Pydantic từ gói `agent_contract`.
2. Không tự sửa protocol; muốn đổi phải tạo issue và tăng version.
3. Không hard-code API key hay LLM provider; không commit `.env`.
4. Agent chỉ làm action thuộc capability đã khai báo.
5. Chỉ **PM** được gọi agent khác qua module conductor (mục 7b, Prompt 8); 9 agent còn lại **không gọi agent khác**, chỉ gợi ý qua `next_actions`.
6. Mọi lỗi trả về `AgentError`; agent không được ném exception ra ngoài `handle()`.
7. Mỗi agent có unit test, README, `.env.example`, demo script.

## 2.8 Agent API v1

Mô hình triển khai: mỗi agent chạy trên một máy riêng, các máy gọi nhau qua mạng LAN bằng HTTP. Monorepo chỉ là cách chia sẻ mã nguồn; lúc chạy, máy nào chỉ khởi động agent của máy đó. Không có thư mục dùng chung giữa các máy, artifact đi qua API của agent.

Bảng endpoint tiêu chuẩn:

| Endpoint | Xác thực | Mô tả |
| --- | --- | --- |
| `GET /health` | Không | Trả về `{status, agent_id, role, contract_version, agent_version}` |
| `GET /agent` | Bearer Token | Trả về `AgentIdentity` của agent |
| `GET /state` | Bearer Token | Trả về `AgentState` hiện thời của agent |
| `POST /tasks` | Bearer Token | Nhận `TaskRequest`, trả `TaskResult` (đồng bộ, áp `timeout_seconds`, idempotent theo `task_id`, concurrency lock: trả 409 nếu đang bận) |
| `GET /tasks/{task_id}` | Bearer Token | Trả `TaskResult` đã lưu từ `data/results/<task_id>.json` hoặc 404 |
| `GET /artifacts` | Bearer Token | Trả danh sách `list[ArtifactRef]` của agent |
| `GET /artifacts/{artifact_id}/content` | Bearer Token | Tải nội dung file artifact, kèm header `X-Checksum-SHA256`, chặn path traversal |
| `POST /artifacts/inbox` | Bearer Token | Multipart form (`metadata`: JSON ArtifactRef, `file`: binary), kiểm tra sha256 và `MAX_UPLOAD_MB`, ghi vào `artifacts/inbox/<origin_agent_id>/` |

**Quy tắc bảo mật và xác thực:**
- Mọi endpoint trừ `GET /health` bắt buộc phải có header `Authorization: Bearer <AGENT_API_KEY>`.
- Token được kiểm tra bằng hàm an toàn `hmac.compare_digest`. Nếu sai hoặc thiếu token, trả HTTP 401 Unauthorized và **tuyệt đối không ghi token vào log**.

## 2.9 Pipeline actions (Contract v1.1.0)

| Role | Action | Capability | Tạo artifact | Đầu vào |
| --- | --- | --- | --- | --- |
| PO | `define_backlog` | `backlog_management` | `BACKLOG` | brief |
| PO | `accept_release` | `acceptance` | `DOCUMENTATION` (verdict) | `TEST_REPORT`, `DEPLOYMENT` |
| BA | `write_requirements` | `requirement_analysis` | `REQUIREMENT` | `BACKLOG`, brief |
| BA | `write_user_stories` | `user_story` | `USER_STORY` | `REQUIREMENT` |
| SA | `design_architecture` | `system_architecture` | `ARCHITECTURE` | `REQUIREMENT`, `USER_STORY` |
| DBA | `design_schema` | `schema_design` | `DATABASE_SCHEMA` | `ARCHITECTURE`, `REQUIREMENT` |
| DEV | `implement_project`, `implement_feature` | `coding` | `SOURCE_CODE` (.zip) | `ARCHITECTURE`, `DATABASE_SCHEMA`, `USER_STORY` |
| DEV | `fix_bugs` | `bug_fixing` | `SOURCE_CODE` (.zip) | `SOURCE_CODE`, `TEST_REPORT` hoặc `SECURITY_REPORT` |
| TESTER | `create_test_cases` | `test_case_generation` | `TEST_CASE` | `USER_STORY` |
| TESTER | `run_test_suite` | `automated_testing` | `TEST_REPORT` (verdict) | `SOURCE_CODE`, `TEST_CASE` |
| SECURITY | `security_review` | `security_review` | `SECURITY_REPORT` (verdict) | `SOURCE_CODE`, `ARCHITECTURE` |
| DEVOPS | `generate_ci_config`, `build_image`, `deploy`, `health_check` | như mục 2.3 | `DEPLOYMENT` | `SOURCE_CODE` |
| SCRUM_MASTER | `track_blockers` | `blocker_tracking` | `DOCUMENTATION` | nhật ký sự kiện dự án |
| SCRUM_MASTER | `run_sprint_review` | `sprint_management` | `DOCUMENTATION` | nhật ký sự kiện sprint |
| SCRUM_MASTER | `run_retrospective` | `retrospective` | `DOCUMENTATION` | nhật ký sự kiện dự án |

**Quy ước verdict:**
`outputs[0]` của TESTER, SECURITY và PO (nghiệm thu) có cấu trúc dạng:
```json
{
  "verdict": "PASS" | "FAIL",
  "findings": [
    {
      "severity": "LOW" | "MEDIUM" | "HIGH" | "CRITICAL",
      "title": "Tên phát hiện",
      "detail": "Mô tả chi tiết"
    }
  ]
}
```
PM chỉ đọc `verdict` và `severity`, không đọc văn bản tự do để đưa ra quyết định chuyển tiếp hay kích hoạt vòng sửa lỗi.

**Quy ước SOURCE_CODE:**
Mã nguồn được chuyển giao giữa các máy độc lập dưới dạng một file nén `.zip` (artifact `SOURCE_CODE`), hoặc thông qua Pull Request GitHub (`github://owner/repo/pull/<n>`).

## 2.10 Mở rộng System Phase (Contract v1.2.0)

### Các System Model (system_models.py)
- `WorkerRegistration`: Đăng ký Worker Host (`worker_id`, `machine_id`, `base_url`, `agents`, `contract_version`, `worker_version`).
- `Heartbeat`: Nhịp tim báo cáo trạng thái Worker (`worker_id`, `agents: list[{agent_id, status, current_task}]`, `running_tasks`, `sent_at`).
- `DispatchRequest`: Giao task bất đồng bộ từ PC1 tới Worker (`TaskRequest` + `callback_url`, `attempt`).
- `DispatchAck`: Phản hồi 202 Accepted từ Worker (`task_id`, `accepted`, `reason`).
- `RunStatus`: Trạng thái vòng đời chạy workflow (`CREATED`, `PLANNING`, `AWAITING_APPROVAL`, `RUNNING`, `BLOCKED`, `COMPLETED`, `FAILED`, `CANCELLED`).
- `DispatchState`: Trạng thái điều phối task (`QUEUED`, `ASSIGNED`, `RUNNING`, `RETRY_WAIT`, `COMPLETED`, `FAILED`, `BLOCKED`, `CANCELLED`).
- `WorkflowRun`: Thực thể tiến trình chạy toàn bộ vòng đời phân phối phần mềm.
- `RunTask`: Thực thể nhiệm vụ trong một run, lưu trữ trạng thái dispatch, version khóa lạc quan.
- `Approval`: Quản lý phê duyệt tại các cổng duyệt (`gate`, `status`, `decided_by`, `decided_at`).
- `AuditEvent`: Nhật ký kiểm toán ghi nhận toàn bộ thao tác hệ thống và gọi adapter.
- `SystemEventType`: Các loại sự kiện hệ thống (`WORKER_ONLINE`, `WORKER_OFFLINE`, `TASK_DISPATCHED`, `TASK_STARTED`, `TASK_COMPLETED`, `TASK_FAILED`, `TASK_BLOCKED`, `APPROVAL_REQUESTED`, `APPROVAL_DECIDED`, `RUN_STARTED`, `RUN_COMPLETED`, `RUN_FAILED`).

### Ánh xạ trạng thái điều phối (DISPATCH_TO_TASK_STATUS)

| `dispatch_state` (DB Control Node) | `TaskStatus` (Contract) |
| --- | --- |
| `QUEUED`, `ASSIGNED`, `RETRY_WAIT` | `PENDING` |
| `RUNNING` | `RUNNING` |
| `COMPLETED` | `COMPLETED` |
| `FAILED` | `FAILED` |
| `BLOCKED` | `BLOCKED` |
| `CANCELLED` | `CANCELLED` |

### Mở rộng URI Artifact (ArtifactRef.uri)
Bên cạnh đường dẫn tương đối an toàn (không tuyệt đối, không chứa `..`), cho phép đúng 3 dạng URI định danh:
1. `github://owner/repo/pull/<n>` (Pull Request)
2. `github://owner/repo/commit/<sha>` (Commit SHA)
3. `artifact://<id>` (Artifact ID trong Artifact Store trên PC1)

### Bảng phân bổ 10 Agent mặc định (AGENT_REGISTRY_DEFAULT)

| agent_id | role | Máy | Runner |
| --- | --- | --- | --- |
| `pm-01` | PM | PC1 | LocalWorker |
| `devops-01` | DEVOPS | PC1 | LocalWorker |
| `sa-01` | SA | PC2 | Worker Host |
| `dba-01` | DBA | PC2 | Worker Host |
| `ba-01` | BA | PC3 | Worker Host |
| `po-01` | PO | PC3 | Worker Host |
| `dev-01` | DEV | PC4 | Worker Host |
| `tester-01` | TESTER | PC4 | Worker Host |
| `scrum_master-01` | SCRUM_MASTER | PC5 | Worker Host |
| `security-01` | SECURITY | PC5 | Worker Host |



