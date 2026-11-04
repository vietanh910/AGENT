# Thiết kế Mạng và Cấu hình Kết nối 5 Máy (docs/network.md)

Tài liệu quy định kiến trúc mạng LAN, phân bổ địa chỉ IP, chính sách tường lửa (firewall), bảo mật kết nối và giải pháp mở rộng qua mạng riêng ảo (VPN/Tailscale) cho hệ thống AI-Team gồm 1 Control Node và 4 Worker Host.

---

## 1. Sơ đồ Tô-pô Mạng (LAN Topology)

```
                            [ Router LAN / Switch Gigabit ]
                                          │
            ┌─────────────────────────────┼─────────────────────────────┐
            │                             │                             │
    [ 192.168.1.101 ]             [ 192.168.1.102 ]             [ 192.168.1.103 ]
       PC1 CONTROL                   PC2 WORKER                    PC3 WORKER
    ┌─────────────────┐           ┌─────────────────┐           ┌─────────────────┐
    │ Control Gateway │           │ Worker Host     │           │ Worker Host     │
    │ Orchestrator    │           │ Agents: sa-01,  │           │ Agents: ba-01,  │
    │ MySQL 8.4 (3307)│           │         dba-01  │           │         po-01   │
    │ Redis 7.4 (6379)│           └─────────────────┘           └─────────────────┘
    │ Local: PM,DevOps│                   │                             │
    └─────────────────┘                   │                             │
            │                             │                             │
            └─────────────────────────────┼─────────────────────────────┘
                                          │
            ┌─────────────────────────────┴─────────────────────────────┐
            │                                                           │
    [ 192.168.1.104 ]                                           [ 192.168.1.105 ]
       PC4 WORKER                                                  PC5 WORKER
    ┌─────────────────┐                                         ┌─────────────────┐
    │ Worker Host     │                                         │ Worker Host     │
    │ Agents: dev-01, │                                         │ Agents: scrum-01│
    │      tester-01  │                                         │      security-01│
    └─────────────────┘                                         └─────────────────┘
```

---

## 2. Bảng Phân bổ IP Tĩnh và DHCP Reservation

Để đảm bảo kết nối ổn định lâu dài giữa các tiến trình điều phối và worker, tất cả 5 máy vật lý **bắt buộc cấu hình DHCP Reservation** trên Router (hoặc gán Static IP cố định trên card mạng Ethernet):

| Máy | Định danh máy | Vai trò | IP Đề xuất (LAN) | Cổng lắng nghe | Giao thức |
|:---:|:---:|---|:---:|:---:|:---:|
| **PC1** | `PC1` | Control Node (Gateway, Orchestrator, DB, Redis) | `192.168.1.101` | **8000** (HTTP)<br>`3307` (MySQL nội bộ)<br>`6379` (Redis nội bộ) | HTTP/JSON<br>TCP<br>TCP |
| **PC2** | `PC2` | Worker Host (`sa-01`, `dba-01`) | `192.168.1.102` | **8000** | HTTP/JSON |
| **PC3** | `PC3` | Worker Host (`ba-01`, `po-01`) | `192.168.1.103` | **8000** | HTTP/JSON |
| **PC4** | `PC4` | Worker Host (`dev-01`, `tester-01`) | `192.168.1.104` | **8000** | HTTP/JSON |
| **PC5** | `PC5` | Worker Host (`scrum_master-01`, `security-01`) | `192.168.1.105` | **8000** | HTTP/JSON |

> [!NOTE]
> Khi thử nghiệm nhiều Worker trên cùng một máy vật lý (Local Multi-Worker Mode), các worker sử dụng cổng riêng biệt: PC2 (`8002`), PC3 (`8003`), PC4 (`8004`), PC5 (`8005`) tại `127.0.0.1`.

---

## 3. Chính sách Tường lửa (Firewall Rules) và Cô lập Dữ liệu

1. **Nguyên tắc cô lập cơ sở dữ liệu**:
   - Dịch vụ **MySQL (cổng 3307)** và **Redis (cổng 6379)** chỉ được triển khai trên **PC1**.
   - Cổng 3307 và 6379 **chỉ bind tại `127.0.0.1`** trên PC1 (hoặc chỉ cho phép kết nối nội bộ từ localhost). Các máy Worker PC2..PC5 **không bao giờ** kết nối trực tiếp vào MySQL/Redis mà chỉ trao đổi qua API Gateway của PC1.
   - Tuyệt đối không mở cổng 3307 hoặc 6379 ra Internet hoặc dải mạng công cộng.

2. **Chính sách Tường lửa trên PC1 (Control Node)**:
   - Mở Inbound cổng `8000/TCP` chỉ cho dải mạng LAN nội bộ (ví dụ: `192.168.1.0/24`).
   - Lệnh PowerShell trên Windows (PC1):
     ```powershell
     New-NetFirewallRule -DisplayName "AI-Team Control Gateway" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -RemoteAddress 192.168.1.0/24
     ```

3. **Chính sách Tường lửa trên Worker (PC2..PC5)**:
   - Mở Inbound cổng `8000/TCP` chỉ cho phép địa chỉ IP của PC1 (`192.168.1.101`):
     ```powershell
     New-NetFirewallRule -DisplayName "AI-Team Worker Host" -Direction Inbound -LocalPort 8000 -Protocol TCP -Action Allow -RemoteAddress 192.168.1.101
     ```

---

## 4. Xác thực và Bảo mật Token theo Từng Máy

- Mỗi Worker Host sở hữu một **Secret Bearer Token riêng biệt** được lưu trong `control/workers.yaml` trên PC1 và file `worker.yaml` trên Worker tương ứng:
  - PC2: `worker_pc2_secret_token_change_me`
  - PC3: `worker_pc3_secret_token_change_me`
  - PC4: `worker_pc4_secret_token_change_me`
  - PC5: `worker_pc5_secret_token_change_me`
- **Nguyên tắc bảo vệ**:
  - Khi một Worker bị xâm nhập, kẻ tấn công chỉ có thể truy cập phạm vi của 2 agent trên máy đó, không thể can thiệp sang agent máy khác và **bị cấm gọi endpoint ADMIN** (trả về 403 Forbidden và ghi log cảnh báo an ninh).
  - PC1 lưu `ADMIN_TOKEN` độc lập cho người quản trị; Worker không bao giờ nắm giữ token này.

---

## 5. Kết nối Khác Mạng qua VPN (Tailscale)

Khi 5 máy vật lý nằm ở các văn phòng, gia đình khác nhau hoặc qua Internet, giải pháp khuyến nghị là sử dụng mạng riêng ảo **Tailscale (WireGuard Mesh VPN)**:

1. **Cài đặt Tailscale**:
   - Cài đặt Tailscale client trên cả 5 máy: `tailscale up`.
   - Tất cả 5 máy đăng nhập vào cùng một Tailnet riêng tư.

2. **Ánh xạ địa chỉ IP Tailscale**:
   - Mỗi máy nhận một địa chỉ IP nội bộ trong dải `100.x.y.z` ổn định:
     - PC1: `100.64.0.1` (hostname: `pc1-control`)
     - PC2: `100.64.0.2` (hostname: `pc2-worker`)
     - PC3: `100.64.0.3` (hostname: `pc3-worker`)
     - PC4: `100.64.0.4` (hostname: `pc4-worker`)
     - PC5: `100.64.0.5` (hostname: `pc5-worker`)
3. **Cập nhật cấu hình**:
   - Trong `worker.yaml` của PC2..PC5, đổi `control_url: "http://100.64.0.1:8000"`.
   - Toàn bộ lưu lượng qua Tailscale được mã hóa đầu-cuối (End-to-End Encryption bằng ChaCha20-Poly1305), vượt NAT/Firewall mà không cần mở cổng modem.

---

## 6. Yêu cầu Đồng bộ Thời gian (NTP & Clock Skew)

- **Ngưỡng cho phép**: Độ lệch thời gian giữa PC1 và các Worker Host **không được vượt quá 2.0 giây**.
- **Lý do**: Kiểm tra thời gian hết hạn (deadline), tính toán timeout, chuỗi băm Merkle của Audit Log, và xác thực outbox replay yêu cầu đồng hồ thống nhất.
- **Cấu hình NTP**:
  - Trên Windows:
    ```powershell
    w32tm /config /manualpeerlist:"pool.ntp.org" /syncfromflags:manual /reliable:yes /update
    w32tm /resync
    ```
  - Kiểm tra độ lệch tự động qua công cụ: `python scripts/lan_check.py`.
