# Kế hoạch triển khai hệ thống: PC1 Control Node và 4 Worker

*Tài liệu giao việc cho Antigravity, giai đoạn System Phase. Tách riêng khỏi tài liệu "Kế hoạch xây dựng 10 Agent và Agent Contract". Cập nhật: 03/11/2026.*

## 1. Mục tiêu và quan hệ với tài liệu trước

**Mục tiêu**: người dùng nhập một yêu cầu. PM lập kế hoạch, hệ thống tạo Project, Milestone, Issue và Sub-Issue trên Linear. Orchestrator trên PC1 giao việc qua LAN cho 10 agent, mã nguồn nằm trên GitHub, sản phẩm được kiểm thử, kiểm tra bảo mật, deploy staging và tiến độ cập nhật lại trên Linear.

**Phân bố máy**: PC1 là Control Node (Orchestrator, Gateway, Registry, MySQL, Redis, cùng hai agent PM và DevOps). PC2 đến PC5 mỗi máy chạy một **Worker Host** chứa 2 agent (SA+DBA, BA+PO, DEV+TESTER, SCRUM\_MASTER+SECURITY).

**Cái gì dùng lại, cái gì thay thế**

| Hạng mục trong tài liệu "Kế hoạch 10 Agent" | Trạng thái | Ghi chú |
| --- | --- | --- |
| Prompt 0 đến 4 (Contract, Template, PM, DevOps) | Dùng nguyên | Nền tảng của mọi thứ ở đây |
| Prompt 5, 6 (`validate_agent`, chạy cục bộ) | Dùng nguyên | Mở rộng ở S12 |
| Prompt 7 (Agent API v1, xác thực, `lan_client`) | Giữ để kiểm thử từng agent | Không phải đường chạy chính nữa |
| Prompt 9 (pipeline action cho 8 agent còn lại) | Dùng, chỉnh phần mã nguồn | Mã nguồn đi qua GitHub (xem S6) |
| Prompt 8 (PM Conductor) | **Thay thế** | Bằng Orchestrator LangGraph (S9) |
| Prompt 10 đến 13 (Tracker nội bộ, Worker kéo việc, Planner, E2E) | **Thay thế** | Bằng PostgreSQL + Redis + Linear theo kế hoạch này |

## 2. Điều chỉnh so với kế hoạch gốc

| # | Kế hoạch gốc | Điều chỉnh | Lý do |
| --- | --- | --- | --- |
| 1 | Thứ tự: Gateway (giai đoạn 3), Registry (4), rồi DB và Redis (5) | Làm **DB và Redis trước** Gateway | Registry, heartbeat và task đều cần lưu bền ngay từ đầu |
| 2 | PC1 gửi task, Worker gửi kết quả về | Chốt thành **bất đồng bộ**: `POST /tasks` trả 202, kết quả gửi qua callback, Worker có hộp thư đi (outbox) gửi lại đến khi PC1 xác nhận | Mạng LAN rớt giữa chừng không được làm mất kết quả |
| 3 | Role `DEVELOPER`, ID `developer-01`, `scrum-01` | Dùng role theo Contract hiện có (`DEV`, `SCRUM_MASTER`), ID `dev-01`, `scrum_master-01` | Không đổi enum đã chốt, tránh vỡ validator |
| 4 | Trạng thái task ASSIGNED, RETRY | Bảng DB có cột `dispatch_state` riêng, Contract giữ `TaskStatus` cũ, có bảng ánh xạ (mục 4) | Không phá Contract |
| 5 | Chưa nói kho artifact | Mã nguồn qua GitHub, tài liệu và báo cáo qua **Artifact Store** trên PC1 | Hết nhu cầu chuyển file zip giữa các máy |
| 6 | Linear webhook | Webhook là **tùy chọn** (qua tunnel), mặc định **polling**; Linear chỉ là bản phản chiếu, lỗi Linear không được chặn workflow | Webhook cần địa chỉ truy cập được từ internet, máy trong LAN thường không có |
| 7 | Ghi thẳng lên Linear | Đồng bộ qua hàng đợi, gộp ghi, backoff, giới hạn số issue mỗi run | Theo nguồn bên thứ ba, gói Free của Linear giới hạn khoảng 250 issue và 2 team, API khoảng 2.000 request mỗi giờ (cần kiểm tra trên trang giá và tài liệu của Linear) |
| 8 | PM tự quyết kế hoạch rồi chạy | PM trả kế hoạch có cấu trúc, **mã kiểm tra** (role và action hợp lệ, DAG không chu trình, giới hạn số task, khung quy trình bắt buộc); lỗi thì dùng kế hoạch mẫu | LLM miễn phí hay trả sai |
| 9 | LangGraph là chuỗi node | Thêm **vòng lập lịch theo phụ thuộc** (DAG), trạng thái bền trong MySQL, phê duyệt qua trạng thái run; gate, vòng sửa lỗi, giới hạn do mã tất định | Cần chạy nhiều task song song và khôi phục khi PC1 khởi động lại |
| 10 | Ứng dụng PC1 chạy trong Docker | MySQL và Redis trong Docker Compose, **ứng dụng chạy trực tiếp trên máy (venv)** | DevOps cần gọi Docker; mount `docker.sock` vào container gần như trao quyền root cho container |
| 11 | Một `WORKER_AUTH_SECRET` chung | Mỗi worker một secret riêng, callback cũng xác thực, MySQL và Redis chỉ lắng nghe nội bộ | Một máy bị lộ không kéo theo cả hệ thống |
| 12 | PM, DevOps trên PC1 gọi như worker khác | Gọi trực tiếp trong tiến trình qua `LocalWorker`, cùng interface `WorkerClient` với `RemoteWorker` (HTTP) | Orchestrator không cần phân biệt local hay remote |
| 13 | Bốn thư mục `pcN-worker` | **Một** `worker/` dùng chung, mỗi máy chỉ khác `worker.yaml` | Không sao chép mã bốn lần |
| 14 | `contracts/` ở gốc repo | Giữ `contract/agent_contract` đã có | Không phá những gì đã làm |

**Điều chỉnh bổ sung về cơ sở dữ liệu**: hệ thống dùng **MySQL 8.4 LTS** thay cho PostgreSQL theo yêu cầu của bạn. Code yêu cầu tối thiểu MySQL 8.0.19 và chạy được trên cả 8.0 lẫn 8.4; vì 8.0 đã hết hỗ trợ từ 30/4/2026 nên bản triển khai là 8.4. Các thay đổi kéo theo: dùng kiểu `JSON` thay `JSONB`, không dùng kiểu mảng (đã có bảng phụ thuộc riêng), migration phải nhỏ và chạy lại được vì MySQL không có DDL giao dịch, giao việc dùng `SELECT ... FOR UPDATE SKIP LOCKED` hoặc cập nhật theo `version`, và LangGraph chỉ có checkpointer MySQL do cộng đồng viết nên không được để run hay task phụ thuộc sống còn vào nó (chi tiết ở S3 và S9).

## 3. Kiến trúc chốt

```
Người dùng: 1 yêu cầu
      │
      ▼
PC1 CONTROL NODE (chạy trực tiếp trên máy)         Docker Compose: MySQL, Redis
  Gateway (FastAPI) · Registry · Task Manager · Dispatcher
  Orchestrator (LangGraph, trạng thái bền trong MySQL)
  Artifact Store · Linear Adapter · GitHub Adapter · Audit
  PM Agent + DevOps Agent (LocalWorker)
      │  HTTP/JSON qua LAN: giao task (202), callback kết quả, heartbeat
      ├─────────────┬─────────────┬─────────────┐
      ▼             ▼             ▼             ▼
   PC2 Worker    PC3 Worker    PC4 Worker    PC5 Worker
   SA + DBA      BA + PO       DEV + TESTER  SCRUM_MASTER + SECURITY

Linear: bản phản chiếu công việc (bạn theo dõi ở đây)
GitHub: mã nguồn, Pull Request, CI
```

Quy tắc: các agent **không bao giờ gọi nhau**; chỉ Orchestrator trên PC1 giao việc và quyết định bước tiếp theo. MySQL là nguồn sự thật của trạng thái vận hành; Linear chỉ phản chiếu.

**Bảng đăng ký agent**

| agent\_id | role | Máy | Chạy qua |
| --- | --- | --- | --- |
| `pm-01` | PM | PC1 | LocalWorker |
| `devops-01` | DEVOPS | PC1 | LocalWorker |
| `sa-01`, `dba-01` | SA, DBA | PC2 | Worker Host |
| `ba-01`, `po-01` | BA, PO | PC3 | Worker Host |
| `dev-01`, `tester-01` | DEV, TESTER | PC4 | Worker Host |
| `scrum_master-01`, `security-01` | SCRUM\_MASTER, SECURITY | PC5 | Worker Host |

**Cấu trúc repo**

```
ai-team/
├── contract/                 # đã có: agent_contract (thêm system_models ở S2)
├── agent-template/  agents/  # đã có: 10 agent
├── control/                  # PC1: Control Node
│   ├── app/
│   │   ├── main.py
│   │   ├── api/              # routes: runs, tasks, workers, agents, artifacts, webhooks
│   │   ├── orchestrator/     # graph, nodes, edges, state, scheduler, guards
│   │   ├── services/         # registry, task_manager, dispatcher, approval, plan_service
│   │   ├── integrations/     # linear/, github/
│   │   ├── database/         # models, repositories, session
│   │   ├── queue/            # redis, events
│   │   ├── security/         # authentication, permissions, secrets
│   │   └── observability/    # logging, audit
│   ├── migrations/  tests/  docker-compose.yml  .env.example  README.md
├── worker/                   # Worker Host dùng chung cho PC2 đến PC5
│   ├── host/                 # main, api, runner, outbox, heartbeat, config
│   └── tests/  worker.example.yaml
├── process/software_delivery.yaml
├── scripts/                  # check_env, lan_check, start_worker, demo_dispatch
└── docs/                     # architecture, network, deployment, RUNBOOK, SYSTEM_PLAN
```

## 4. Giao thức PC1 và Worker (chốt trước khi viết code)

**API trên PC1** (tiền tố `/api/v1`, trừ `/health`)

| API | Chức năng |
| --- | --- |
| `GET /health` | Kiểm tra PC1 |
| `POST /runs`, `GET /runs/{id}`, `GET /runs/{id}/tasks`, `GET /runs/{id}/plan` | Tạo run và xem tiến độ |
| `POST /runs/{id}/approve`, `/reject`, `/cancel`, `/pause`, `/resume` | Điều khiển run (token ADMIN) |
| `POST /runs/{id}/tasks/{task_id}/unblock` | Cấp thêm ngữ cảnh cho task bị chặn |
| `GET /agents` | Danh sách 10 agent và trạng thái |
| `POST /workers/register`, `POST /workers/heartbeat` | Worker đăng ký, báo còn sống |
| `POST /tasks/{id}/result` | Worker gửi `TaskResult` |
| `POST/GET /artifacts` | Kho artifact |
| `POST /webhooks/linear`, `/webhooks/github` | Tùy chọn, có kiểm tra chữ ký |

**API trên Worker Host** (cổng 8000 trên mỗi máy)

| API | Chức năng |
| --- | --- |
| `GET /health` | Kiểm tra Worker |
| `GET /agents`, `GET /agents/{id}` | Danh sách và thông tin agent trên máy |
| `POST /tasks` | Nhận `DispatchRequest`, trả **202** ngay (hoặc 409 nếu agent bận, 422 nếu sai role hoặc action) |
| `GET /tasks/{id}` | Trạng thái task |
| `POST /tasks/{id}/cancel` | Hủy |

**Luồng một task**: PC1 `POST {worker}/tasks` (kèm `callback_url`) → 202 → Worker chạy agent → ghi kết quả vào outbox → `POST /api/v1/tasks/{id}/result` → PC1 trả 200 xác nhận thì Worker mới xóa khỏi outbox; chưa xác nhận thì gửi lại với backoff.

**Heartbeat**: Worker gọi `register` khi khởi động, sau đó heartbeat mỗi 10 giây, kèm trạng thái từng agent và danh sách task đang chạy. Quá 45 giây không có heartbeat thì Worker bị coi là OFFLINE; PC1 không giao việc mới, task đang chạy trên Worker đó được xử lý theo chính sách thử lại.

**Idempotency**: mọi thao tác theo `task_id`. Giao trùng thì Worker trả lại kết quả cũ; nhận kết quả trùng thì PC1 bỏ qua.

**Ánh xạ trạng thái**

| `dispatch_state` (DB của PC1) | `TaskStatus` (Contract) |
| --- | --- |
| `QUEUED`, `ASSIGNED`, `RETRY_WAIT` | `PENDING` |
| `RUNNING` | `RUNNING` |
| `COMPLETED` | `COMPLETED` |
| `FAILED` | `FAILED` |
| `BLOCKED` | `BLOCKED` |
| `CANCELLED` | `CANCELLED` |

## 5. Kế hoạch theo giai đoạn

| Giai đoạn gốc | Prompt | Nội dung | Quy mô |
| --- | --- | --- | --- |
| 1 Chuẩn bị môi trường | S1 | Kiểm tra môi trường, Docker Compose, khung `control/` | Nhỏ |
| 2 Agent Contract | S2 | Contract mở rộng, ID agent, pipeline action, ánh xạ trạng thái | Vừa |
| 5 MySQL và Redis (làm trước) | S3 | Model, migration, repository, queue, khóa, event | Vừa |
| 3 và 4 Gateway, Registry | S4 | API trên PC1, đăng ký, heartbeat, xác thực | Vừa |
| 4 Task Manager (cộng Worker) | S5 | Dispatcher bất đồng bộ, Worker Host, worker giả | Lớn |
| (bổ sung) | S6 | Artifact Store, GitHub Adapter, công cụ git cho agent | Lớn |
| 6 Linear | S7 | Linear Adapter, đồng bộ, polling | Lớn |
| 7 PM Agent | S8 | Lập kế hoạch, kiểm tra kế hoạch, kế hoạch mẫu | Vừa |
| 8 LangGraph | S9 | Orchestrator, vòng sửa lỗi, phê duyệt, khôi phục | Lớn |
| 9 DevOps Agent | S10 | CI, merge, build, deploy staging, health check | Vừa |
| 10 Hoàn thiện | S11 | Audit, log, chịu lỗi, quyền, nhiều run | Vừa |
| 11 Triển khai Worker | S12 | Cài đặt PC2 đến PC5, nghiệm thu từ đầu đến cuối | Vừa |

S7 và S8 có thể làm song song sau S3. Mỗi prompt kết thúc bằng: chạy test, duyệt diff, cập nhật `CHANGELOG`, gắn tag; chỉ giao prompt kế tiếp khi prompt trước đã xanh.

## 6. Prompt giao cho Antigravity

**Cách dùng**: lưu tài liệu này vào repo thành `docs/SYSTEM_PLAN.md` (mục 2 đến 4 là thiết kế đã chốt). Dán **S0** vào file rules của workspace, rồi giao lần lượt S1 đến S12.

### S0 — Bối cảnh và quy tắc (dán một lần)

```
Bạn là kỹ sư Python senior xây dựng hệ thống "ai-team" giai đoạn System Phase. Repo đã có contract/agent_contract, agent-template/ và 10 agent trong agents/ (từ giai đoạn xây dựng Agent). Nhiệm vụ của bạn là xây Control Node cho PC1 và Worker Host cho PC2 đến PC5.

Tài liệu bắt buộc đọc trước mỗi nhiệm vụ: docs/SYSTEM_PLAN.md (đặc biệt mục 2, 3, 4) và docs/CONTRACT.md. Đây là nguồn sự thật. Nếu thấy mâu thuẫn hoặc thiếu, DỪNG và hỏi tôi, không tự quyết.

Quy tắc:
1. MySQL là nguồn sự thật của trạng thái vận hành. Redis chỉ làm hàng đợi, khóa, sự kiện. Linear chỉ là bản phản chiếu; lỗi Linear không bao giờ được chặn hay làm hỏng workflow.
2. Các agent không gọi nhau. Chỉ Orchestrator trên PC1 giao việc.
3. Mọi cuộc gọi ra ngoài (Linear, GitHub, LLM, Worker) đi qua adapter có timeout, retry có giới hạn, backoff và ghi audit. Không retry vô hạn.
4. Không đổi enum và model có sẵn trong agent_contract; chỉ thêm. Mọi thay đổi Contract phải ghi CHANGELOG và tăng version minor.
5. Không hard-code secret, URL, ID. Mọi cấu hình qua .env hoặc file yaml có bản .example. Không ghi secret vào log.
6. Test chạy offline: dùng fake và fixture ghi sẵn cho Linear, GitHub, LLM, Worker. Chỉ test đánh dấu @pytest.mark.integration mới được chạm dịch vụ thật.
7. Ghim phiên bản mọi thư viện. Với LangGraph, SQLAlchemy, Alembic, FastAPI: đọc tài liệu của đúng phiên bản đã cài trước khi dùng API, vì các API này thay đổi nhanh. Không dựa vào trí nhớ.
8. Python 3.11+, type hint đầy đủ, Pydantic v2, IO dùng async khi hợp lý, thời gian dùng UTC có timezone.
9. Làm đúng phạm vi của prompt, không thêm tính năng. Cuối mỗi nhiệm vụ: liệt kê file đã tạo hoặc sửa, lệnh chạy test, kết quả test, những điểm bạn chưa chắc chắn.
```

### S1 — Môi trường PC1 và khung dự án (giai đoạn 1)

```
Đọc docs/SYSTEM_PLAN.md mục 3. Chuẩn bị nền tảng cho PC1. Không viết logic nghiệp vụ.

1. Tạo khung thư mục control/ và worker/ theo cấu trúc ở mục 3 (file rỗng có docstring mô tả vai trò). Cập nhật .gitignore: .env, control/data/, worker/data/, *.pem, worker.yaml, workers.yaml.
2. control/docker-compose.yml: MySQL (image 8.4 LTS làm mặc định, đổi được sang 8.0 qua biến MYSQL_VERSION để test) và Redis, ghim tag ảnh cụ thể, có healthcheck, volume bền, mật khẩu lấy từ .env, cổng chỉ bind 127.0.0.1 (không mở ra LAN). KHÔNG đưa ứng dụng control vào compose: ứng dụng chạy trực tiếp trên máy.
3. control/.env.example đầy đủ: APP_ENV, APP_HOST, APP_PORT, DATABASE_URL, REDIS_URL, LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, LLM_BASE_URL, LINEAR_API_KEY, LINEAR_TEAM_ID, GITHUB_TOKEN, GITHUB_REPOSITORY, ADMIN_TOKEN, WORKERS_FILE, HEARTBEAT_TIMEOUT_SECONDS=45, APPROVAL_MODE=true, MAX_TASKS_PER_RUN=60, MAX_FIX_LOOPS=3, MAX_PARALLEL_TASKS=2, RUN_TIMEOUT_MINUTES=240, LINEAR_DRY_RUN=true, ALLOW_PROD_DEPLOY=false. Chú thích từng biến.
4. scripts/check_env.py: kiểm tra Python >= 3.11, git, docker và docker compose, các cổng cần dùng còn trống, kết nối MySQL và Redis, các khóa bắt buộc trong .env. Cờ --online thử gọi chỉ-đọc tới Linear và GitHub để xác nhận token. In bảng PASS/FAIL kèm gợi ý sửa, exit code khác 0 nếu FAIL. Không tự cài đặt thứ gì.
5. control/app/main.py: ứng dụng FastAPI tối thiểu có GET /health, đọc cấu hình bằng pydantic-settings.
6. control/README.md: các bước cài trên Windows (WSL2), macOS và Linux, thứ tự khởi động.

Tiêu chí xong: docker compose up -d lên healthy; python scripts/check_env.py báo PASS; uvicorn chạy và /health trả 200 từ máy khác trong LAN; pytest cho check_env (mock) xanh.
```

### S2 — Contract mở rộng cho hệ thống (giai đoạn 2)

```
Đọc docs/SYSTEM_PLAN.md mục 2 và 4, và docs/CONTRACT.md. Mở rộng contract/agent_contract. Chỉ thêm, không đổi cái có sẵn. Tăng CONTRACT_VERSION lên minor kế tiếp (dự kiến 1.1.0), cập nhật CHANGELOG và docs/CONTRACT.md.

1. system_models.py (Pydantic v2): WorkerRegistration (worker_id, machine_id, base_url, agents: list[AgentIdentity], contract_version, worker_version); Heartbeat (worker_id, agents: list[{agent_id, status, current_task}], running_tasks, sent_at); DispatchRequest (TaskRequest kèm callback_url, attempt); DispatchAck (task_id, accepted, reason); RunStatus (CREATED, PLANNING, AWAITING_APPROVAL, RUNNING, BLOCKED, COMPLETED, FAILED, CANCELLED); WorkflowRun; RunTask; DispatchState (QUEUED, ASSIGNED, RUNNING, RETRY_WAIT, COMPLETED, FAILED, BLOCKED, CANCELLED); Approval (gate, status, decided_by, decided_at); AuditEvent (agent_id hoặc actor, action, target, task_id, run_id, result, timestamp); SystemEventType.
2. Hằng DISPATCH_TO_TASK_STATUS theo bảng ánh xạ ở mục 4.
3. ErrorCode bổ sung: WORKER_OFFLINE, DISPATCH_FAILED, LINEAR_ERROR, GITHUB_ERROR, APPROVAL_REQUIRED.
4. registry.py: AGENT_REGISTRY_DEFAULT gồm 10 agent đúng bảng ở mục 3 (agent_id, role, machine, runner). Validator agent_id dạng <role viết thường>-NN, với role đúng enum.
5. Đăng ký pipeline action vào ACTION_MAP và CAPABILITY_REGISTRY (nếu chưa có), theo danh sách (role: action -> artifact):
   PO: define_backlog -> BACKLOG, accept_release -> DOCUMENTATION (verdict). BA: write_requirements -> REQUIREMENT, write_user_stories -> USER_STORY. SA: design_architecture -> ARCHITECTURE. DBA: design_schema -> DATABASE_SCHEMA. DEV: implement_project, implement_feature, fix_bugs -> SOURCE_CODE. TESTER: create_test_cases -> TEST_CASE, run_test_suite -> TEST_REPORT (verdict). SECURITY: security_review -> SECURITY_REPORT (verdict). DEVOPS: generate_ci_config, build_image, deploy, health_check -> DEPLOYMENT. SCRUM_MASTER: track_blockers, run_sprint_review, run_retrospective -> DOCUMENTATION. PM: giữ 6 action hiện có.
6. Model Verdict (verdict PASS hoặc FAIL, findings: list[{severity LOW|MEDIUM|HIGH|CRITICAL, title, detail}]) dùng cho outputs[0] của TESTER, SECURITY và PO (accept_release).
7. ArtifactRef.uri: ngoài đường dẫn tương đối, cho phép đúng ba dạng github://owner/repo/pull/<n>, github://owner/repo/commit/<sha> và artifact://<id>. Validator vẫn chặn ".." và đường dẫn tuyệt đối.
8. Xuất JSON Schema, cập nhật tools/validate_agent.py cho ID mới và pipeline action.

Test: ví dụ JSON hợp lệ và không hợp lệ cho từng model; mọi action trong ACTION_MAP có capability thuộc role; ánh xạ trạng thái đủ mọi giá trị DispatchState; uri hợp lệ và không hợp lệ.

Tiêu chí xong: pytest xanh; validate_agent chạy được trên pm và devops với Contract mới; 10 agent trong registry có ID đúng.
```

### S3 — MySQL và Redis (giai đoạn 5, làm trước Gateway)

Khác biệt MySQL so với PostgreSQL cần biết trước: dùng kiểu `JSON` (không có `JSONB`, không có kiểu mảng), DDL không nằm trong giao dịch nên migration lỗi giữa chừng để lại trạng thái dở, và LangGraph không có checkpointer MySQL chính thức, chỉ có gói cộng đồng `langgraph-checkpoint-mysql`. Gói này mô phỏng bản Postgres, cần `autocommit=True`, và tác giả cảnh báo MySQL từ bản 9.6 đã bỏ hàm MD5 trong generated column mà chưa có đường nâng cấp, nên ghim **MySQL 8.4 LTS** làm bản triển khai, vì MySQL 8.0 đã hết hỗ trợ từ 30/4/2026. Code yêu cầu tối thiểu MySQL 8.0.19 và phải chạy được trên cả 8.0 lẫn 8.4, không dùng 9.6 trở lên (kiểm tra bản còn được hỗ trợ trước khi ghim). Vì vậy trạng thái run và task luôn nằm trong bảng của chính chúng ta, checkpointer chỉ là phần bổ sung.

```
Đọc docs/SYSTEM_PLAN.md mục 4 và contract/agent_contract. Xây tầng lưu trữ cho control/ bằng MySQL (tối thiểu 8.0.19, triển khai trên 8.4 LTS) và Redis. Chưa viết API.

1. control/app/database/: SQLAlchemy 2.x, driver PyMySQL (đồng bộ, gọi qua threadpool trong FastAPI) hoặc driver async nếu thư viện đó có bản đang được bảo trì (kiểm tra trước khi dùng, ghim phiên bản). Với MySQL 8.4, plugin mysql_native_password bị tắt mặc định nên driver phải dùng được caching_sha2_password (PyMySQL thường cần thêm gói cryptography; kiểm tra và ghi vào requirements). Mọi bảng InnoDB, charset utf8mb4 và collation thống nhất cho cả database lẫn bảng. Thời gian lưu UTC dạng DATETIME(6), đặt time_zone của session là UTC. Khóa chính dùng chuỗi có độ dài giới hạn cho task_id và run_id, chú ý giới hạn độ dài chỉ mục của utf8mb4.
2. Các bảng: workers, agents, workflow_runs, tasks, task_dependencies, task_attempts, artifacts, approvals, audit_logs, agent_events, linear_links (entity_type, local_id, linear_id, content_hash, last_synced_at). Cột dữ liệu linh hoạt dùng kiểu JSON của MySQL. Không dùng kiểu mảng: phụ thuộc đã có bảng task_dependencies, các danh sách khác dùng JSON hoặc bảng phụ. Bảng tasks gồm: task_id, run_id, agent_id, role, action, instruction, context (JSON), dispatch_state, attempts, max_attempts, timeout_seconds, deadline, result (JSON), error (JSON), linear_issue_id, version (khóa lạc quan), created_at, updated_at.
3. Xử lý khác biệt MySQL trong code:
   - Không có RETURNING trong mọi phiên bản: ghi xong thì đọc lại trong cùng giao dịch.
   - Chọn việc để giao hoặc nhận kết quả dùng giao dịch ngắn với SELECT ... FOR UPDATE SKIP LOCKED (MySQL 8) hoặc cập nhật có điều kiện theo version; đặt sẵn mức cô lập và timeout khóa.
   - Gặp deadlock hoặc lock wait timeout (mã lỗi 1213 và 1205) thì retry có giới hạn và backoff.
   - Chỉ mục trên giá trị trong cột JSON phải qua generated column.
   - Tách một wrapper cho mọi truy vấn để bắt lỗi kết nối và tự kết nối lại (pool_pre_ping, pool_recycle).
4. repositories.py: toàn bộ truy cập DB nằm ở đây, không có SQL ở nơi khác. Chuyển dispatch_state chỉ theo bảng chuyển hợp lệ (chuyển sai ném lỗi), kiểm tra version khi ghi. Hàm tính danh sách task READY (mọi phụ thuộc COMPLETED, state QUEUED hoặc RETRY_WAIT đã tới hạn).
5. migrations/ bằng Alembic. Vì MySQL không có DDL giao dịch: mỗi migration nhỏ, viết sao cho chạy lại được (kiểm tra tồn tại trước khi tạo), có hướng dẫn khôi phục trong docstring. upgrade và downgrade phải chạy được trên database trống.
6. control/app/queue/redis.py: hàng đợi theo agent, khóa phân tán lock:task:{id} có TTL và gia hạn, Redis Streams cho sự kiện theo run (events:run:{run_id}) kèm consumer group, khóa idempotency, bộ đếm giới hạn tốc độ. Mỗi thành phần có giao diện trừu tượng và bản fake (fakeredis) cho test.
7. events.py: phát và đọc sự kiện (WORKER_ONLINE, WORKER_OFFLINE, TASK_DISPATCHED, TASK_STARTED, TASK_COMPLETED, TASK_FAILED, APPROVAL_REQUESTED, v.v.), mọi sự kiện cũng ghi vào agent_events.
8. LangGraph checkpoint: chỉ chuẩn bị. Cài gói cộng đồng langgraph-checkpoint-mysql (ghim phiên bản, bắt buộc autocommit=True khi tự tạo kết nối), viết hàm tạo checkpointer và smoke test gọi setup(), put, get. Bọc sau một interface CheckpointStore để thay được. KHÔNG để run hay task phụ thuộc vào checkpoint: trạng thái thật nằm ở các bảng trên, khi khởi động lại orchestrator phải dựng lại được từ bảng. Kiểm tra gói có chạy đúng với MySQL 8.4 ghim ở S1; nếu không, báo lại cho tôi thay vì tự đổi hướng.

Test (MySQL thật qua compose, Redis fake): chuyển trạng thái hợp lệ và không hợp lệ, xung đột version, tính READY với phụ thuộc, hai tiến trình cùng giành một task chỉ một thắng, giả lập deadlock và retry, sự kiện không mất khi consumer khởi động lại, khởi động lại ứng dụng thì task QUEUED còn nguyên, smoke test checkpointer; chạy lại toàn bộ bộ test trên cả MySQL 8.0 và 8.4 (hai service compose, chọn bằng MYSQL_VERSION) để bảo đảm không dùng tính năng chỉ có ở 8.4; khi kết nối thì kiểm tra phiên bản server, dưới 8.0.19 hoặc từ 9.6 trở lên thì dừng với thông báo rõ.

Tiêu chí xong: alembic upgrade head và downgrade base chạy sạch trên database trống; pytest xanh; script scripts/db_smoke.py tạo run, task, phụ thuộc rồi in danh sách READY.
```

### S4 — Gateway, Registry và Heartbeat (giai đoạn 3 và nửa đầu giai đoạn 4)

```
Đọc docs/SYSTEM_PLAN.md mục 4. Xây FastAPI Gateway và Agent Registry trên PC1, dùng tầng lưu trữ ở S3. Chưa giao task thật (để S5).

1. control/app/api/routes/: các endpoint theo bảng "API trên PC1" ở mục 4: GET /health (không cần token); POST /api/v1/runs, GET /api/v1/runs/{id}, GET /api/v1/runs/{id}/tasks, GET /api/v1/runs/{id}/plan, POST .../approve, reject, cancel, pause, resume, POST /api/v1/runs/{id}/tasks/{task_id}/unblock; GET /api/v1/agents; POST /api/v1/workers/register; POST /api/v1/workers/heartbeat; POST /api/v1/tasks/{id}/result; POST /api/v1/webhooks/linear và /github (chỉ kiểm tra chữ ký và ghi sự kiện, chưa hành động). Các endpoint của run và task ở giai đoạn này chỉ ghi và đọc DB, logic chạy để S5, S8, S9.
2. Xác thực (security/authentication.py): mỗi worker một secret trong control/workers.yaml (gitignore, có workers.example.yaml), gửi bằng Bearer, so sánh bằng hmac.compare_digest. Token ADMIN (từ .env) riêng cho các endpoint điều khiển run và phê duyệt. Worker chỉ được gọi endpoint của chính nó và chỉ gửi kết quả cho task đã giao cho agent của nó; trái quy tắc trả 403 và ghi audit. Không ghi token vào log.
3. services/registry.py: nạp AGENT_REGISTRY_DEFAULT vào bảng agents (mọi agent OFFLINE, riêng pm-01 và devops-01 do tiến trình PC1 tự báo ONLINE). register cập nhật worker và danh sách agent (từ chối agent_id không có trong registry hoặc role không khớp). heartbeat cập nhật last_seen, trạng thái từng agent và danh sách task đang chạy.
4. services/heartbeat.py: tác vụ nền mỗi 5 giây đánh dấu worker OFFLINE khi quá HEARTBEAT_TIMEOUT_SECONDS (45); phát sự kiện WORKER_ONLINE và WORKER_OFFLINE; ghi audit. Worker trở lại thì ONLINE lại. Chỉ chuyển trạng thái khi thực sự đổi, không phát sự kiện lặp.
5. Định dạng lỗi thống nhất dùng AgentError/ErrorCode của Contract cho mọi phản hồi lỗi. Giới hạn kích thước body, timeout, CORS tắt mặc định.
6. api/dependencies.py và security/permissions.py: ma trận ai được gọi endpoint nào (WORKER, ADMIN, WEBHOOK).
7. GET /api/v1/agents trả bảng: agent_id, role, machine, trạng thái, last_seen, current_task.

Test (httpx AsyncClient, MySQL thật, Redis fake): không token hoặc sai token trả 401; worker A gửi kết quả của task thuộc worker B trả 403; register agent sai role bị từ chối; heartbeat cập nhật trạng thái; ngừng heartbeat quá hạn thì OFFLINE và có sự kiện đúng một lần; online lại; webhook sai chữ ký trả 401; GET /agents đủ 10 dòng.

Tiêu chí xong: pytest xanh; từ máy khác trong LAN gọi được /health; dùng curl đăng ký một worker giả và thấy 2 agent chuyển ONLINE, tắt curl heartbeat thì sau 45 giây về OFFLINE.
```

### S5 — Task Manager, Dispatcher và Worker Host (nửa sau giai đoạn 4 và giai đoạn 11 sớm)

Làm Worker Host ngay ở đây, cùng worker giả, để kiểm thử giao tiếp hai chiều trước khi có agent thật.

```
Đọc docs/SYSTEM_PLAN.md mục 4. Xây ba phần: Task Manager và Dispatcher trên PC1, Worker Host dùng chung cho PC2 đến PC5, và worker giả để test. Giao thức đã chốt ở mục 4: giao task bất đồng bộ (202), kết quả qua callback, outbox phía Worker, idempotency theo task_id.

A. Control, control/app/services/
1. task_manager.py: tạo task và phụ thuộc từ kế hoạch, tính READY, chuyển dispatch_state theo bảng ánh xạ, ghi task_attempts, tính deadline từ timeout_seconds.
2. dispatcher.py: vòng lặp chọn task READY, tìm agent theo role (v1 mỗi role một agent), kiểm tra worker ONLINE và agent chưa bận (tối đa 1 task đồng thời mỗi agent, giới hạn chung MAX_PARALLEL_TASKS), lấy khóa Redis theo task, POST {worker}/tasks với DispatchRequest và callback_url. Phản hồi 202 thì ASSIGNED; 409 thì trả về QUEUED chờ; 422 thì FAILED không retry; lỗi mạng hoặc timeout thì RETRY_WAIT với backoff có trần. Task chuyển RUNNING khi nhận heartbeat hoặc sự kiện bắt đầu từ worker.
3. LocalWorker và RemoteWorker cùng interface WorkerClient (dispatch, cancel, get_status). LocalWorker chạy agent trong tiến trình PC1 (pm-01 và devops-01) bằng BaseAgent.handle() trong threadpool; Orchestrator không phân biệt hai loại.
4. result_handler: endpoint POST /tasks/{id}/result gọi vào đây. Idempotent (kết quả trùng thì trả 200 và bỏ qua); kiểm tra task_id, agent_id, TaskResult hợp lệ; lưu kết quả, phát sự kiện. FAILED có error.retryable=true và còn lượt thì RETRY_WAIT (giữ nguyên task_id), hết lượt thì FAILED; retryable=false hoặc BLOCKED thì giữ trạng thái đó và báo orchestrator.
5. Watchdog: task quá deadline thì gửi cancel tới worker và đặt FAILED mã TIMEOUT, retryable. Worker OFFLINE trong khi có task RUNNING: sau thời hạn ân hạn thì đặt lại thành RETRY_WAIT để giao lại (cùng task_id), ghi audit.
6. Hủy task và hủy cả run.

B. Worker Host, worker/host/ (một mã nguồn, mỗi máy một worker.yaml)
1. worker.yaml: worker_id, machine_id, control_url, secret, listen host và port, danh sách agent (đường dẫn thư mục agents/<tên>). worker.example.yaml cho từng PC.
2. api.py theo bảng "API trên Worker Host" ở mục 4. POST /tasks kiểm tra role và action thuộc agent đó, rồi trả 202 ngay; 409 nếu agent đang bận; giao trùng task_id thì trả lại trạng thái hoặc kết quả cũ.
3. runner.py: mỗi agent một hàng đợi kích thước 1, chạy BaseAgent.handle() trong thread riêng; hỗ trợ hủy phối hợp (cờ hủy), chặn ở timeout của task.
4. outbox.py: kết quả ghi vào SQLite cục bộ (worker/data/outbox.db) TRƯỚC khi gửi; gửi POST /api/v1/tasks/{id}/result kèm secret; chỉ xóa khi PC1 trả 200; chưa được thì gửi lại với backoff và chạy lại sau khi worker khởi động lại. Bộ nhớ đệm kết quả theo task_id cho idempotency.
5. heartbeat.py: register khi khởi động (thử lại nếu PC1 chưa lên), rồi heartbeat 10 giây, kèm trạng thái từng agent và task đang chạy.
6. Tắt sạch: ngừng nhận task, chờ task đang chạy hoặc báo kết quả CANCELLED, gửi hết outbox rồi thoát.

C. Worker giả: worker/fake_agent.py cấu hình được hành vi theo action: thành công sau N giây kèm artifact giả, lỗi retryable N lần rồi thành công, lỗi không retryable, BLOCKED MISSING_CONTEXT, treo mãi (để thử timeout), sập tiến trình. Dùng để test mà không cần LLM.

Test (PC1 thật, worker thật chạy trên port ngẫu nhiên, agent giả): giao và nhận kết quả đúng thứ tự phụ thuộc; mất callback thì outbox gửi lại; giao trùng task_id không chạy hai lần; callback trùng bị bỏ qua; worker tắt giữa chừng thì task được giao lại sau ân hạn; timeout thì hủy và retry; lỗi không retryable không retry; hủy task; khởi động lại PC1 thì task đang chờ không bị mất hay giao trùng; không giao cho worker OFFLINE.

Tiêu chí xong: pytest xanh; scripts/demo_dispatch.py tạo 5 task có phụ thuộc phân cho 2 worker giả và tất cả hoàn thành đúng thứ tự, kể cả khi tắt một worker giữa chừng rồi bật lại.
```

### S6 — Artifact Store, GitHub Adapter và công cụ git cho agent (bổ sung)

Mã nguồn đi qua GitHub, tài liệu và báo cáo đi qua kho artifact trên PC1. Hai thứ này thay cho việc chuyển file zip giữa các máy ở kế hoạch trước.

```
Đọc docs/SYSTEM_PLAN.md mục 2 và 4. Làm ba phần, không đổi giao thức đã chốt.

A. Artifact Store trên PC1
1. POST /api/v1/artifacts (multipart: metadata ArtifactRef và file), GET /api/v1/artifacts/{id}/content kèm header X-Checksum-SHA256, GET /api/v1/runs/{id}/artifacts?type=&latest=true. Giới hạn kích thước từ .env, tên file được làm sạch, tính và so sha256, phiên bản mới không ghi đè bản cũ. File lưu trong control/data/artifacts/<run>/<id>-<tên>, metadata trong bảng artifacts; uri dạng artifact://<id>.
2. Chọn artifact đầu vào cho một task: một hàm duy nhất select_input_artifacts(task) dựa theo loại đầu vào của action trong ACTION_MAP, lấy phiên bản mới nhất trong run.

B. Worker Host dùng kho artifact (sửa worker/host/runner.py)
1. Trước khi chạy: với mỗi input_artifact có uri artifact://, tải về agent/artifacts/inbox/, kiểm tra sha256; lỗi thì trả BLOCKED MISSING_CONTEXT. Uri github:// thì không tải, agent tự dùng git.
2. Sau khi chạy: upload mọi artifact trong artifacts/index.json của agent lên kho, ghi uri artifact://<id> vào TaskResult trước khi đưa vào outbox. Upload lỗi thì kết quả không được coi là hoàn thành, thử lại theo outbox.

C. GitHub
1. control/app/integrations/github/: client httpx (xác thực bằng token, timeout, backoff khi 429, dùng ETag cho polling), repositories.py, pull_requests.py. Chức năng: lấy thông tin repo, liệt kê PR, đọc trạng thái CI (check runs) của một commit hoặc PR, merge PR (squash) CHỈ qua hàm có tham số policy_ok do orchestrator truyền vào, kiểm tra chữ ký webhook. Mọi lệnh ghi qua adapter đều ghi audit. Bản fake cho test.
2. agent-template/tools/git_workspace.py dùng chung cho DEV, TESTER, SECURITY: clone repo vào workspace, tạo branch agent/<task_id>, commit, push, tạo PR (qua API GitHub), checkout branch của PR để test. Chạy lệnh git qua tools/terminal.py (whitelist, không shell). Không bao giờ force push, không push lên main, tên branch được làm sạch, tôn trọng DRY_RUN.
3. Cập nhật hành vi action của DEV: implement_project, implement_feature, fix_bugs làm việc trên repo thật (LLM trả danh sách file như cũ, code kiểm tra đường dẫn rồi ghi vào workspace, commit, push, mở PR). Artifact SOURCE_CODE có uri github://owner/repo/pull/<n> và metadata gồm commit sha. TESTER và SECURITY checkout đúng PR hoặc commit từ input_artifacts rồi chạy kiểm tra trong workspace; kết quả test lấy từ mã thoát và log của lệnh.
4. Phân quyền token GitHub (ghi vào docs/deployment.md): PC4 dùng token fine-grained chỉ cho repo này với quyền contents và pull requests, không có quyền merge vào main hoặc quản trị; PC1 dùng token có quyền merge, dành cho DevOps qua adapter.

Test: kho artifact (checksum, tên độc hại, phiên bản bất biến, chọn bản mới nhất); worker tải đầu vào và upload đầu ra đúng; git_workspace với repo remote cục bộ dạng bare (file://) và API GitHub giả (respx): branch, commit, push, PR, checkout; không thể push main hay force push; adapter fake cho merge có policy.

Tiêu chí xong: pytest xanh; demo: DEV giả đẩy branch lên repo cục bộ và mở PR giả, TESTER giả checkout PR đó và tạo TEST_REPORT upload lên kho, PC1 liệt kê được cả hai artifact.
```

### S7 — Linear Adapter và đồng bộ (giai đoạn 6)

```
Đọc docs/SYSTEM_PLAN.md mục 2 (dòng 6 và 7). Xây tích hợp Linear. Linear là bản phản chiếu: không bao giờ chặn hay làm hỏng workflow.

1. Đọc tài liệu GraphQL hiện hành của Linear và dùng introspection để xác nhận tên query, mutation và trường trước khi viết code. Không dựa vào trí nhớ.
2. control/app/integrations/linear/client.py: GraphQL qua httpx, xác thực bằng API key, timeout, retry có giới hạn với backoff khi 429 hoặc 5xx, đọc header giới hạn tốc độ nếu có. Interface WorkTracker (Protocol): ensure_project, ensure_milestone, upsert_issue (kể cả sub-issue), set_status, set_labels, add_comment, add_relation (blocks), attach_link, list_changes_since. Bản FakeWorkTracker trong bộ nhớ. LINEAR_DRY_RUN=true thì chỉ ghi log hành động.
3. Ánh xạ cấu hình trong control/linear.yaml: dispatch_state sang tên trạng thái workflow của team Linear (ví dụ Backlog, Todo, In Progress, Done, Canceled), cộng nhãn blocked và needs-approval. Khi khởi động lấy ID các trạng thái và nhãn rồi cache; thiếu cái nào thì dừng với thông báo liệt kê chính xác phần thiếu. Agent không phải người dùng Linear: gắn nhãn theo role (agent:dev, agent:tester, ...) và ghi tên agent, máy chạy trong mô tả và comment, không dùng assignee.
4. Đồng bộ ra Linear (sync.py): mọi thay đổi (tạo project, milestone, task, đổi trạng thái, kết quả, artifact, phê duyệt) được đưa vào hàng đợi đồng bộ trong Redis cùng bản ghi bền. Worker đồng bộ xử lý nền, idempotent qua bảng linear_links và content_hash (chạy lại không sinh trùng), gộp và làm chậm ghi (LINEAR_MAX_REQ_PER_MIN, đặt mặc định thận trọng), backoff khi 429, chuyển vào dead-letter sau K lần lỗi và ghi audit. Lỗi Linear không bao giờ ném lên orchestrator.
5. Mẫu nội dung issue: Agent chịu trách nhiệm, máy chạy, mô tả yêu cầu, Acceptance Criteria, phụ thuộc, task_id và run_id, link PR hoặc artifact. Comment kết quả có dạng "[Developer Agent • dev-01] tóm tắt, lần thử, thời lượng, link".
6. Chiều ngược (từ người dùng): polling list_changes_since mỗi LINEAR_POLL_SECONDS (mặc định 20) để nhận phê duyệt (chuyển issue cổng sang Done hoặc comment /approve) và thông tin bổ sung cho task bị chặn. Chỉ các issue cổng phê duyệt và lệnh trong comment mới có hiệu lực; mọi chỉnh sửa tay khác trên issue do agent quản lý chỉ được ghi log. Webhook là tùy chọn (LINEAR_WEBHOOK_ENABLED), dùng khi có tunnel, có kiểm tra chữ ký.
7. Giới hạn: MAX_LINEAR_ISSUES_PER_RUN, và scripts/linear_cleanup.py <run> để hủy hoặc lưu trữ issue của run cũ (gói miễn phí giới hạn số issue).

Test (fixture ghi sẵn, không gọi mạng): chạy đồng bộ hai lần không sinh trùng; 429 thì backoff rồi thành công; thiếu trạng thái trong workflow thì dừng sớm với thông báo rõ; phê duyệt qua polling; dead-letter sau K lần lỗi; Linear sập thì workflow vẫn chạy; vượt MAX_LINEAR_ISSUES_PER_RUN thì dừng đồng bộ, ghi cảnh báo.

Tiêu chí xong: pytest xanh; với API key thật (test integration): tạo Project, Milestone, Issue, Sub-Issue, quan hệ blocks, đổi trạng thái và comment hiện đúng trên giao diện Linear; chạy lại không tạo trùng; tôi kiểm tra bằng mắt.
```

### S8 — PM lập kế hoạch từ một prompt (giai đoạn 7)

```
Đọc docs/SYSTEM_PLAN.md mục 2 (dòng 8), docs/CONTRACT.md và agents/pm/. Nguyên tắc: PM (LLM) viết nội dung kế hoạch, nhưng khung quy trình, giới hạn và kiểm tra hợp lệ do mã tất định quyết định.

1. process/software_delivery.yaml: khung quy trình bắt buộc gồm các epic và task mẫu theo thứ tự: po.define_backlog, ba.write_requirements, ba.write_user_stories, GATE approve_scope, sa.design_architecture, dba.design_schema, tester.create_test_cases, dev.implement_project (bộ khung và CI cơ bản), phần động cho từng user story (dev.implement_feature, sau mỗi sprint có tester.run_test_suite và scrum_master.run_sprint_review), security.security_review, devops.generate_ci_config, devops.build_image, devops.deploy (staging), health_check, GATE approve_prod (chỉ khi triển khai production), po.accept_release, scrum_master.run_retrospective, pm.report_status. Mỗi mục khai báo role, action, phụ thuộc, loại artifact đầu vào và đầu ra, priority, max_attempts, timeout_seconds, acceptance_criteria_template.
2. control/app/services/plan_service.py:
   - Nhận prompt của người dùng, gọi PM qua LocalWorker với action create_plan, đầu ra là ProjectPlan có cấu trúc (milestone, task gồm role, action, title, description, acceptance_criteria, depends_on, rủi ro).
   - plan_validator.py kiểm tra bằng mã: mọi cặp (role, action) có trong ACTION_MAP; capability thuộc đúng role; đồ thị phụ thuộc không có chu trình và không có tham chiếu tới task không tồn tại; số task <= MAX_TASKS_PER_RUN; mọi task có tiêu chí chấp nhận và loại artifact đầu ra suy ra từ ACTION_MAP; các bước bắt buộc của khung quy trình đều có mặt và đúng thứ tự (PM không được bỏ test, security hay phê duyệt).
   - PM trả kế hoạch sai thì yêu cầu sửa lại tối đa 2 lần kèm danh sách lỗi; vẫn sai thì dùng khung quy trình mẫu, LLM chỉ điền mô tả và tiêu chí chấp nhận. Không bao giờ chạy kế hoạch chưa qua validator.
   - expand_stories(run, artifact USER_STORY): sau khi BA xong, đọc dữ liệu có cấu trúc của user story, tạo task implement_feature tuần tự (một DEV), gom vào sprint (SPRINT_SIZE=3), giới hạn MAX_STORIES=8, thêm task kiểm thử và review cuối mỗi sprint.
3. Lưu kế hoạch vào DB (task, phụ thuộc, milestone) và đưa việc tạo Project, Milestone, Issue, Sub-Issue, quan hệ blocks vào hàng đợi đồng bộ Linear. Mỗi task ghi rõ agent chịu trách nhiệm và máy chạy.
4. Approval Mode (APPROVAL_MODE=true mặc định): sau khi lập kế hoạch run ở AWAITING_APPROVAL, bạn xem kế hoạch trên Linear hoặc qua GET /api/v1/runs/{id}/plan rồi gọi POST /runs/{id}/approve. Chế độ tự động (APPROVAL_MODE=false) bỏ qua bước này nhưng vẫn giữ cổng approve_prod.
5. POST /api/v1/runs nhận prompt, tạo run, bắt đầu lập kế hoạch bất đồng bộ và trả run_id ngay. CLI: python -m cli run start "...", run status <id>, run plan <id>, run approve <id>.

Test (PM giả trả JSON định sẵn): kế hoạch hợp lệ; role hoặc action sai; chu trình phụ thuộc; vượt số task; thiếu bước test hoặc security; LLM trả JSON hỏng thì dùng khung mẫu; prompt tiếng Việt và tiếng Anh; expand_stories tôn trọng MAX_STORIES và tuần tự; Approval Mode chặn tới khi approve.

Tiêu chí xong: pytest xanh; python -m cli run start "Xây dựng sàn thương mại điện tử nông sản" đưa run vào AWAITING_APPROVAL với danh sách task hợp lệ trong MySQL, và (với Linear thật hoặc fake) đã được phản chiếu đủ cấu trúc.
```

### S9 — LangGraph Orchestrator (giai đoạn 8)

```
Đọc docs/SYSTEM_PLAN.md mục 2 (dòng 9) và mục 4. Xây Orchestrator trong control/app/orchestrator/. Trước khi viết, đọc tài liệu của đúng phiên bản LangGraph đã ghim (graph, conditional edges, persistence) và gói langgraph-checkpoint-mysql đã chuẩn bị ở S3.

Nguyên tắc: trạng thái thật của run và task nằm trong các bảng MySQL. Graph chỉ là bộ điều khiển luồng; khi PC1 khởi động lại, orchestrator dựng lại hoàn toàn từ bảng và chạy tiếp, kể cả khi checkpointer không dùng được. Dùng checkpointer là bổ sung, bọc sau CheckpointStore. Phê duyệt đi qua trạng thái run AWAITING_APPROVAL cộng sự kiện (không bắt buộc dùng interrupt của LangGraph).

1. state.py: state tối thiểu (run_id, bộ đếm vòng sửa lỗi, cờ dừng). nodes.py, edges.py, graph.py với các node: receive_request, pm_planning, create_linear_project (đưa vào hàng đợi đồng bộ), await_plan_approval, schedule, dispatch_ready, wait_for_event, process_result, route, retry_task, handle_failure, fix_loop, gate, devops_deploy, health_check, finalize.
2. Vòng lập lịch theo phụ thuộc, không phải chuỗi tuyến tính: schedule tính mọi task READY từ DB, dispatch tôn trọng MAX_PARALLEL_TASKS và 1 task mỗi agent; wait_for_event chờ sự kiện trên Redis Streams (có timeout, không giữ HTTP request); process_result cập nhật DB và quyết định bước kế.
3. Quy tắc định tuyến tất định:
   - TESTER verdict FAIL: tạo task BUG cho DEV (fix_bugs, đầu vào TEST_REPORT và SOURCE_CODE mới nhất), rồi chạy lại tester.run_test_suite.
   - SECURITY verdict FAIL với finding HIGH hoặc CRITICAL: tương tự, sau đó test lại và review lại.
   - Build FAIL: tạo task sửa cho DEV kèm log. Health check FAIL: không ghi nhận triển khai thành công, tạo task sửa hoặc chặn run.
   - BLOCKED với MISSING_CONTEXT: run sang BLOCKED, ghi comment lên Linear, chờ POST /runs/{id}/tasks/{task_id}/unblock kèm ngữ cảnh bổ sung rồi giao lại.
   - Cổng approve_scope và approve_prod: run AWAITING_APPROVAL tới khi có approve từ token ADMIN hoặc từ polling Linear (S7); reject thì dừng run.
   - Deploy production chỉ khi ALLOW_PROD_DEPLOY=true và cổng approve_prod đã duyệt; staging chạy tự động.
4. Giới hạn bảo vệ trong guards.py: MAX_FIX_LOOPS=3 chung cho cả run, MAX_TASKS_PER_RUN, RUN_TIMEOUT_MINUTES, max_attempts mỗi task. Vượt thì run BLOCKED, không bao giờ chạy vô hạn.
5. Các agent không gọi nhau; artifact đầu vào cho mỗi task do orchestrator chọn bằng select_input_artifacts (S6).
6. Điều khiển run: cancel (gửi cancel tới worker), pause, resume. Mọi chuyển trạng thái run ghi audit.
7. Khôi phục: khi PC1 khởi động, tìm run RUNNING, AWAITING_APPROVAL, BLOCKED và dựng lại vòng lập lịch; task ASSIGNED chưa có xác nhận thì giao lại (cùng task_id, idempotent); task RUNNING chờ heartbeat hoặc kết quả.
8. Nhiều run chạy đồng thời, cô lập với nhau, chia lượt công bằng giữa các run khi tranh nhau agent.

Test (worker giả): chuỗi phụ thuộc tuyến tính; các task độc lập chạy song song trong giới hạn; TESTER FAIL hai lần rồi PASS đúng hai lần fix_bugs; FAIL bốn lần thì run BLOCKED; SECURITY FAIL rồi sửa; worker offline rồi online lại; kill tiến trình PC1 giữa chừng rồi chạy lại: không mất task, không giao trùng; cổng phê duyệt; reject; cancel; vượt giới hạn; hai run đồng thời không lẫn dữ liệu; không dùng checkpointer vẫn khôi phục được.

Tiêu chí xong: pytest xanh; kịch bản end-to-end với worker giả chạy hết run tới COMPLETED; bằng chứng khôi phục sau khi kill PC1.
```

### S10 — DevOps: CI, merge, build, deploy staging (giai đoạn 9)

```
Đọc docs/SYSTEM_PLAN.md và agents/devops/ (Prompt 4 của kế hoạch Agent). DevOps chạy trên PC1 qua LocalWorker. Mọi lệnh hệ thống đi qua tools/terminal.py (whitelist, không shell, giới hạn thư mục, dry-run mặc định). LLM chỉ đề xuất kế hoạch và nội dung file cấu hình, không bao giờ tự sinh lệnh chạy.

1. Điều kiện vào bước deploy do orchestrator kiểm tra bằng mã, không do DevOps tự quyết: TESTER verdict PASS, SECURITY verdict PASS, CI của PR xanh (đọc qua GitHub adapter, polling có timeout). Thiếu một điều kiện thì không giao task deploy.
2. Chuỗi action: devops.generate_ci_config (tạo Dockerfile, docker-compose và workflow CI thành một PR vào repo), chờ CI xanh, merge PR qua adapter với policy_ok do orchestrator truyền, devops.build_image (build từ commit đã merge), đẩy image lên registry nếu REGISTRY_URL được cấu hình (mặc định build và dùng image cục bộ), devops.deploy (staging), devops.health_check.
3. Deploy staging: thư mục deploy/staging/<run_id>/, tên compose project riêng, cổng cấp phát từ dải cấu hình (bộ cấp cổng, tránh đụng nhau giữa các run), file env sinh ngoài repo và không chứa secret của hệ thống. MAX_STAGING_STACKS và lệnh dọn stack cũ.
4. Health check bằng httpx với timeout và retry, tiêu chí đọc từ cấu hình (đường dẫn, mã trạng thái, số lần thử). Chỉ khi PASS mới ghi nhận triển khai thành công. Kết quả trả về: URL ứng dụng, phiên bản (commit sha và tag image), trạng thái, phần cuối log; artifact DEPLOYMENT kèm kế hoạch rollback.
5. Production: chỉ khi ALLOW_PROD_DEPLOY=true và cổng approve_prod đã được duyệt; mặc định luôn dry-run. Thiếu điều kiện thì TaskResult BLOCKED với PERMISSION_DENIED hoặc APPROVAL_REQUIRED, retryable=false.
6. Bảo mật: không mount docker.sock vào container nào; chạy docker CLI trực tiếp trên máy PC1; che secret trong log và kết quả.
7. Cập nhật Linear: issue triển khai nhận comment với URL, phiên bản, trạng thái.

Test (terminal giả, GitHub fake): thiếu điều kiện thì không deploy; CI đỏ thì không merge; build lỗi trả FAILED TOOL_ERROR; health check FAIL thì không báo thành công; cấp cổng không trùng giữa hai run; dry-run không chạy lệnh nào; production bị chặn khi thiếu cờ hoặc phê duyệt. Một test đánh dấu docker triển khai ứng dụng mẫu nhỏ bằng Docker thật.

Tiêu chí xong: pytest xanh; ứng dụng mẫu được build và deploy staging trên PC1, health check PASS, URL hiện trong kết quả run và trên Linear.
```

### S11 — Hoàn thiện vận hành (giai đoạn 10)

```
Đọc docs/SYSTEM_PLAN.md. Hoàn thiện khả năng vận hành lâu dài của Control Node, không thêm tính năng nghiệp vụ.

1. Logging có cấu trúc (JSON) với correlation id run_id, task_id, agent_id, bộ lọc che secret. Audit log (bảng audit_logs, chỉ ghi thêm): mọi giao việc, kết quả, phê duyệt, thay đổi trạng thái run, mọi lệnh gọi ra Linear và GitHub, mọi hành động điều khiển bằng token ADMIN. Có thể thêm hash nối chuỗi để phát hiện sửa dữ liệu.
2. Ma trận xử lý lỗi, mỗi dòng có test: worker offline, LLM timeout (LLM_ERROR, retryable), Linear sập (hàng đợi đồng bộ tiếp tục, workflow không bị ảnh hưởng), GitHub lỗi (task BLOCKED retryable), MySQL mất kết nối (API trả 503, tự kết nối lại, không mất việc), Redis mất kết nối (chế độ suy giảm: orchestrator quay về đọc từ DB theo chu kỳ), mạng bị chia cắt giữa PC1 và worker. Circuit breaker cho Linear, GitHub và từng worker.
3. Không retry vô hạn: backoff có trần, max_attempts mỗi task, ngân sách retry cho cả run, task độc hại (liên tục lỗi) bị cách ly vào BLOCKED.
4. Phân quyền: ActionPolicy (role của agent được làm action nào) kiểm tra ở Dispatcher và khi nhận kết quả; token WORKER không được gọi endpoint ADMIN; token ADMIN tách riêng; lệnh nguy hiểm vẫn do whitelist của từng agent chặn.
5. Nhiều run đồng thời: ba run cùng lúc với worker giả, không lẫn dữ liệu, không đói tài nguyên; giới hạn tổng số run chạy song song.
6. GET /metrics (định dạng văn bản của Prometheus, tối giản): số task theo trạng thái, số worker online, độ trễ hàng đợi đồng bộ Linear, số run theo trạng thái.
7. Sao lưu: scripts/backup.py dùng mysqldump với --single-transaction (không khóa bảng khi sao lưu), mỗi ngày, giữ N bản, kèm hướng dẫn và script khôi phục đã thử trên database trống.
8. Soak test: scripts/soak.py chạy 100 task qua worker giả có lỗi ngẫu nhiên, báo không task nào mất, không task nào chạy trùng.

Tiêu chí xong: pytest xanh; bộ chaos test (kill worker, kill PC1, ngắt Redis, ngắt MySQL, Linear trả 429) đều hồi phục không mất hay trùng task; khôi phục từ bản sao lưu thành công.
```

### S12 — Triển khai Worker PC2 đến PC5 và nghiệm thu từ đầu đến cuối (giai đoạn 11)

```
Đọc docs/SYSTEM_PLAN.md. Đưa hệ thống lên 5 máy thật và chứng minh chạy đúng quy trình.

1. worker/worker.pc2.yaml, pc3, pc4, pc5 (bản .example) với đúng agent từng máy theo bảng đăng ký ở mục 3. scripts/start_worker.py --config worker.yaml khởi động Worker Host; scripts/start_control.py khởi động PC1 (compose, migration, ứng dụng). docs/network.md và docs/deployment.md: IP tĩnh hoặc DHCP reservation, firewall chỉ cho dải LAN, MySQL và Redis chỉ trên PC1, secret riêng từng worker, khi khác mạng thì dùng VPN như Tailscale.
2. scripts/lan_check.py chạy từ PC1: với mỗi worker gọi /health, /agents, thử không token phải bị 401, đo độ trễ, so lệch đồng hồ, so contract_version và agent_version. Xuất bảng PASS/FAIL và docs/lan-check-report.md.
3. scripts/smoke_agents.py: gửi một TaskRequest mẫu cho mọi pipeline action của mọi agent thật (LLM giả) và kiểm tra TaskResult hợp lệ qua agent_contract.
4. tools/check_process.py <run>: đọc audit_logs, tasks và artifacts trong MySQL, báo PASS/FAIL: mọi task COMPLETED có kết quả và artifact đúng loại; thứ tự thực hiện tôn trọng phụ thuộc; mỗi task chỉ do agent đúng role làm; không chuyển trạng thái trái bảng hợp lệ; mọi cổng đã qua phê duyệt; deploy production không chạy khi thiếu cờ hoặc phê duyệt; vòng sửa lỗi không vượt giới hạn; mọi story có test liên quan; Linear phản chiếu đúng số task. Xuất docs/process-report-<run>.md.
5. tests/e2e/: dựng PC1 thật cùng worker giả cho cả 8 agent còn lại, chạy một run mẫu hết đường từ prompt tới COMPLETED, kiểm tra check_process PASS.
6. docs/RUNBOOK.md: thứ tự bật (infra PC1, control, các worker), bắt đầu một run, duyệt cổng, xem tiến độ trên Linear, xử lý BLOCKED, sao lưu và khôi phục MySQL, nâng cấp Contract, xử lý sự cố thường gặp.

Tiêu chí xong: lan_check PASS cho cả 4 worker; smoke_agents PASS; e2e xanh; với LLM thật và một prompt nhỏ (ví dụ ứng dụng todo một trang), run chạy tới COMPLETED ba lần liên tiếp, check_process PASS, Linear thể hiện đủ epic, task, comment, ứng dụng staging chạy được.
```

## 7. Nghiệm thu PC1

PC1 đạt yêu cầu khi cả tám điều sau đều đúng, và mỗi điều có bằng chứng là test hoặc báo cáo tự sinh.

| # | Điều kiện | Bằng chứng | Prompt |
| --- | --- | --- | --- |
| 1 | Nhận prompt và tạo Workflow Run | `POST /runs` trả `run_id`, run có trong MySQL | S8 |
| 2 | PM phân tích và tạo Project trên Linear | Kế hoạch qua validator, Linear phản chiếu đủ cấu trúc | S7, S8 |
| 3 | Đăng ký, giám sát, giao việc cho Worker | `GET /agents` đủ 10 agent, `lan_check` PASS | S4, S5, S12 |
| 4 | Nhận kết quả và cập nhật Linear | Kết quả lưu MySQL, comment và trạng thái đúng trên Linear | S5, S7 |
| 5 | Điều phối nhiều agent theo phụ thuộc | E2E với worker giả, vòng sửa lỗi hoạt động | S9 |
| 6 | DevOps build, deploy, kiểm tra | Staging chạy, health check PASS | S10 |
| 7 | Theo dõi lịch sử và xử lý lỗi | Audit đầy đủ, chaos test hồi phục | S11 |
| 8 | Khôi phục workflow sau khi PC1 khởi động lại | Kill PC1 giữa chừng, run chạy tiếp, không mất hay trùng task | S9, S11 |

**Ma trận kiểm thử theo kế hoạch gốc**: PC1 khởi động (S1, S4), Agent Registry hiển thị 10 agent (S4), Worker đăng ký và heartbeat (S4), PC2 offline thì SA và DBA OFFLINE (S4), giao task đúng agent (S5), nhận kết quả (S5), Linear Adapter (S7), PM Agent (S8), LangGraph điều phối phụ thuộc (S9), retry (S5, S9), GitHub Adapter (S6), DevOps build và deploy staging (S10), health check (S10), khởi động lại PC1 khôi phục workflow (S9, S11).

## 8. Rủi ro và cách xử lý

| Rủi ro | Cách xử lý |
| --- | --- |
| Gói `langgraph-checkpoint-mysql` do cộng đồng duy trì, có thể lệch phiên bản LangGraph hoặc MySQL mới | Ghim phiên bản, bọc sau `CheckpointStore`; trạng thái thật nằm ở bảng của hệ thống nên mất checkpointer vẫn khôi phục được |
| MySQL 9.6 trở lên đã bỏ MD5 trong generated column, gói checkpointer chưa có đường nâng cấp | Code yêu cầu tối thiểu 8.0.19, triển khai trên 8.4 LTS, ghim tối đa 8.4 và kiểm tra phiên bản server khi khởi động; 8.0 chỉ dùng để thử vì không còn bản vá bảo mật |
| MySQL không có DDL giao dịch | Migration nhỏ, chạy lại được, có hướng dẫn khôi phục; sao lưu trước khi nâng cấp |
| Giới hạn gói Free của Linear (theo nguồn bên thứ ba: khoảng 250 issue, 2 team, 2.000 request mỗi giờ; cần kiểm tra) | Đồng bộ bất đồng bộ có giới hạn tốc độ, `MAX_LINEAR_ISSUES_PER_RUN`, `linear_cleanup`, chế độ dry-run |
| Webhook Linear và GitHub cần địa chỉ truy cập được từ internet | Mặc định polling; webhook là tùy chọn qua tunnel |
| LLM miễn phí hay trả sai hoặc chạm rate limit | Validator kiểm tra kế hoạch, kế hoạch mẫu dự phòng, backoff, `MAX_PARALLEL_TASKS` |
| Mất mạng giữa PC1 và Worker | Giao việc 202 cộng callback, outbox, idempotency theo `task_id` |
| PC1 là điểm tập trung và giữ mọi token | Sao lưu MySQL hằng ngày, bảo vệ máy PC1, token riêng cho từng worker, ADMIN tách riêng |
| DevOps chạy lệnh nguy hiểm | Whitelist, không shell, dry-run mặc định, không mount `docker.sock`, production cần cờ và phê duyệt |
| Chất lượng code của DEV phụ thuộc model | Vòng test và sửa lỗi tối đa 3 vòng, kiểm thử bằng lệnh thật, bắt đầu với brief nhỏ |

## 9. Thứ tự giao việc

S0 (một lần), S1, S2, S3, S4, S5, rồi S6 và S7 có thể song song, S8, S9, S10, S11, S12. Sau mỗi prompt: chạy test, duyệt diff, cập nhật `CHANGELOG`, gắn tag. Giao S5 xong là đã có thể thấy toàn bộ giao tiếp 5 máy chạy với worker giả, nên đây là mốc nên dừng lại để kiểm tra bằng mắt trước khi đi tiếp.
