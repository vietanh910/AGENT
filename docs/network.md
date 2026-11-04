# Thiết kế Mạng và Cấu hình Kết nối 5 Máy (docs/network.md)

Tài liệu quy định kiến trúc mạng LAN / Radmin VPN, phân bổ địa chỉ IP, chính sách tường lửa (firewall), bảo mật kết nối và giải pháp kết nối cho hệ thống AI-Team gồm 1 Control Node và 4 Worker Host.

---

## 1. Sơ đồ Tô-pô Mạng (LAN / Radmin VPN Topology)

```
                            [ Mạng Radmin VPN LAN: 26.0.0.0/8 ]
                                           │
            ┌─────────────────────────────┼─────────────────────────────┐
            │                             │                             │
    [ 26.105.63.241 ]             [ 26.28.90.248 ]              [ 26.53.120.228 ]
       PC1 CONTROL                   PC2 WORKER                    PC3 WORKER
    ┌─────────────────┐           ┌─────────────────┐           ┌─────────────────┐
    │ Control Gateway │           │ Worker Host     │           │ Worker Host     │
    │ Orchestrator    │           │ Agents: dev-01, │           │ Agents: po-01,  │
    │ MySQL 8.4 (3307)│           │    scrum_master │           │         sa-01   │
    │ Redis 7.4 (6379)│           │ Port: 8001      │           │ Port: 8001      │
    │ Local: PM,DevOps│           └─────────────────┘           └─────────────────┘
    └─────────────────┘                   │                             │
            │                             │                             │
            └─────────────────────────────┼─────────────────────────────┘
                                           │
            ┌─────────────────────────────┴─────────────────────────────┐
            │                                                           │
    [ 26.222.181.101 ]                                          [ 26.134.25.163 ]
       PC4 WORKER                                                  PC5 WORKER
    ┌─────────────────┐                                         ┌─────────────────┐
    │ Worker Host     │                                         │ Worker Host     │
    │ Agents: ba-01,  │                                         │ Agents: tester, │
    │         dba-01  │                                         │       security  │
    │ Port: 8001      │                                         │ Port: 8001      │
    └─────────────────┘                                         └─────────────────┘
```

---

## 2. Bảng Phân bổ IP Tĩnh và Phân vai Agent

Hệ thống kết nối qua mạng Radmin VPN nội bộ (`26.0.0.0/8`):

| Máy | Định danh máy | Vai trò & Agent Phụ trách | IP Radmin VPN | Cổng lắng nghe | Giao thức |
|:---:|:---:|---|:---:|:---:|:---:|
| **PC1** | `PC1` | Control Node (Gateway, Orchestrator, DB, Redis)<br>Agents cục bộ: `pm-01` (PM), `devops-01` (DEVOPS) | `26.105.63.241` | **8000** (HTTP Gateway)<br>`3307` (MySQL nội bộ)<br>`6379` (Redis nội bộ) | HTTP/JSON<br>TCP<br>TCP |
| **PC2** | `PC2` | Worker Host (`dev-01`, `scrum_master-01`) | `26.28.90.248` | **8001** | HTTP/JSON |
| **PC3** | `PC3` | Worker Host (`po-01`, `sa-01`) | `26.53.120.228` | **8001** | HTTP/JSON |
| **PC4** | `PC4` | Worker Host (`ba-01`, `dba-01`) | `26.222.181.101` | **8001** | HTTP/JSON |
| **PC5** | `PC5` | Worker Host (`tester-01`, `security-01`) | `26.134.25.163` | **8001** | HTTP/JSON |

---

## 3. Chính sách Tường lửa (Firewall Rules) và Cô lập Dữ liệu

1. **Nguyên tắc cô lập cơ sở dữ liệu**:
   - Dịch vụ **MySQL (cổng 3307)** và **Redis (cổng 6379)** chỉ được triển khai trên **PC1**.
   - Cổng 3307 và 6379 **chỉ bind tại `127.0.0.1`** trên PC1. Các máy Worker PC2..PC5 **không bao giờ** kết nối trực tiếp vào MySQL/Redis mà chỉ trao đổi qua API Gateway của PC1.
   - Tuyệt đối không mở cổng 3307 hoặc 6379 ra Internet hoặc dải mạng công cộng.

2. **Chính sách Tường lửa trên PC1 (Control Node)**:
   - Mở Inbound cổng `8000/TCP` cho dải Radmin VPN `26.0.0.0/8`:
   - Lệnh PowerShell trên Windows (PC1 - Run as Administrator):
     ```powershell
     New-NetFirewallRule -DisplayName "AI-Team Control Gateway" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -RemoteAddress 26.0.0.0/8
     ```

3. **Chính sách Tường lửa trên Worker (PC2..PC5)**:
   - Mở Inbound cổng `8001/TCP` cho phép địa chỉ IP của PC1 (`26.105.63.241`):
     ```powershell
     New-NetFirewallRule -DisplayName "AI-Team Worker Host" -Direction Inbound -LocalPort 8001 -Protocol TCP -Action Allow -RemoteAddress 26.105.63.241
     ```

---

## 4. Xác thực và Bảo mật Token theo Từng Máy

- Mỗi Worker Host sở hữu một **Secret Bearer Token riêng biệt** được lưu trong `control/workers.yaml` trên PC1 và file `worker.yaml` trên Worker tương ứng:
  - `PC2`: `worker_pc2_secret_token_change_me`
  - `PC3`: `worker_pc3_secret_token_change_me`
  - `PC4`: `worker_pc4_secret_token_change_me`
  - `PC5`: `worker_pc5_secret_token_change_me`
- Khi Worker gọi API Gateway của PC1 (`/tasks/poll`, `/tasks/result`, `/heartbeat`), Worker gửi secret qua header:
  ```http
  Authorization: Bearer <worker_secret>
  ```
- Khi PC1 gọi Worker Host (`/health`, `/agents`, `/tasks/dispatch`), PC1 cũng gửi secret tương ứng qua header:
  ```http
  Authorization: Bearer <worker_secret>
  ```
- Cơ chế xác thực sử dụng hàm `hmac.compare_digest` để chống tấn công phân tích thời gian (timing attacks).
