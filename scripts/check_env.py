#!/usr/bin/env python3
"""scripts/check_env.py - Kiểm tra môi trường cho PC1 Control Node.

Kiểm tra:
1. Phiên bản Python >= 3.11
2. Git CLI
3. Docker CLI
4. Docker Compose
5. Các cổng cần dùng (8000, 3306, 6379)
6. Kết nối MySQL (8.4 LTS / 8.0)
7. Kết nối Redis
8. Các biến cấu hình bắt buộc trong .env / control/.env
9. (Khi có cờ --online) Kiểm tra chỉ-đọc tới Linear API và GitHub API

In bảng PASS/FAIL kèm gợi ý sửa, thoát mã khác 0 nếu có lỗi.
Không tự ý cài đặt bất kỳ thành phần nào.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def setup_console_encoding() -> None:
    """Đảm bảo console in ra UTF-8 trên Windows."""
    if sys.platform == "win32":
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


class CheckResult:
    """Kết quả kiểm tra từng hạng mục."""

    def __init__(self, name: str, passed: bool, detail: str, hint: str = ""):
        self.name = name
        self.passed = passed
        self.detail = detail
        self.hint = hint


def check_python_version() -> CheckResult:
    """Kiểm tra Python >= 3.11."""
    v = sys.version_info
    ver_str = f"{v[0]}.{v[1]}.{v[2]}"
    if v >= (3, 11):
        return CheckResult(
            name="Python Version",
            passed=True,
            detail=f"Python {ver_str} (>= 3.11)",
        )
    return CheckResult(
        name="Python Version",
        passed=False,
        detail=f"Python {ver_str} không đạt yêu cầu",
        hint="Cài đặt Python 3.11 trở lên từ python.org",
    )


def check_git_cli() -> CheckResult:
    """Kiểm tra git command line tool."""
    git_path = shutil.which("git")
    if not git_path:
        return CheckResult(
            name="Git CLI",
            passed=False,
            detail="Không tìm thấy lệnh git trong PATH",
            hint="Cài đặt Git từ https://git-scm.com và thêm vào biến môi trường PATH",
        )
    try:
        res = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return CheckResult(
            name="Git CLI",
            passed=True,
            detail=res.stdout.strip(),
        )
    except Exception as exc:
        return CheckResult(
            name="Git CLI",
            passed=False,
            detail=f"Lỗi khi thực thi git: {exc}",
            hint="Kiểm tra lại cài đặt Git",
        )


def check_docker_cli() -> CheckResult:
    """Kiểm tra docker command line tool."""
    docker_path = shutil.which("docker")
    if not docker_path:
        return CheckResult(
            name="Docker CLI",
            passed=False,
            detail="Không tìm thấy lệnh docker trong PATH",
            hint="Cài đặt Docker Desktop hoặc Docker Engine để chạy MySQL và Redis",
        )
    try:
        res = subprocess.run(
            ["docker", "--version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return CheckResult(
            name="Docker CLI",
            passed=True,
            detail=res.stdout.strip(),
        )
    except Exception as exc:
        return CheckResult(
            name="Docker CLI",
            passed=False,
            detail=f"Docker cài đặt nhưng gặp lỗi: {exc}",
            hint="Khởi động Docker Desktop hoặc kiểm tra dịch vụ dockerd",
        )


def check_docker_compose() -> CheckResult:
    """Kiểm tra docker compose v2."""
    try:
        res = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return CheckResult(
            name="Docker Compose",
            passed=True,
            detail=res.stdout.strip(),
        )
    except Exception:
        # Thử docker-compose standalone v1
        if shutil.which("docker-compose"):
            try:
                res1 = subprocess.run(
                    ["docker-compose", "--version"],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                return CheckResult(
                    name="Docker Compose",
                    passed=True,
                    detail=res1.stdout.strip(),
                )
            except Exception:
                pass
        return CheckResult(
            name="Docker Compose",
            passed=False,
            detail="Lệnh docker compose không khả dụng",
            hint="Cài đặt Docker Compose v2 (đi kèm Docker Desktop)",
        )


def is_port_in_use(host: str, port: int) -> bool:
    """Kiểm tra một cổng có đang được lắng nghe bởi tiến trình nào không."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        try:
            s.connect((host, port))
            return True
        except (socket.timeout, ConnectionRefusedError, OSError):
            return False


def check_app_port(port: int = 8000) -> CheckResult:
    """Kiểm tra cổng cho Control Node Gateway (8000)."""
    in_use = is_port_in_use("127.0.0.1", port)
    if not in_use:
        return CheckResult(
            name=f"App Port {port}",
            passed=True,
            detail=f"Cổng {port} đang trống, sẵn sàng cho Control Gateway",
        )
    # Nếu đang dùng, kiểm tra xem có phải Control Gateway đang chạy không
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1.5) as resp:
            if resp.status == 200:
                return CheckResult(
                    name=f"App Port {port}",
                    passed=True,
                    detail=f"Cổng {port} đang chạy Control Node Gateway (/health OK)",
                )
    except Exception:
        pass
    return CheckResult(
        name=f"App Port {port}",
        passed=False,
        detail=f"Cổng {port} đang bị chiếm bởi ứng dụng khác",
        hint=f"Tắt ứng dụng chiếm cổng {port} hoặc đổi APP_PORT trong .env",
    )


def parse_database_host_port(db_url: str) -> tuple[str, int]:
    """Phân tích host và port từ DATABASE_URL."""
    if not db_url:
        return "127.0.0.1", 3306
    # Định dạng chuẩn: dialect://user:password@host:port/database
    clean_url = db_url.split("?")[0]
    match = re.search(r"@([^:/]+)(?::(\d+))?", clean_url)
    if match:
        host = match.group(1)
        port = int(match.group(2)) if match.group(2) else 3306
        return host, port
    return "127.0.0.1", 3306


def parse_redis_host_port(redis_url: str) -> tuple[str, int]:
    """Phân tích host và port từ REDIS_URL."""
    if not redis_url:
        return "127.0.0.1", 6379
    match = re.search(r"://([^:/]+)(?::(\d+))?", redis_url)
    if match:
        host = match.group(1)
        port = int(match.group(2)) if match.group(2) else 6379
        return host, port
    return "127.0.0.1", 6379


def check_mysql_connection(db_url: str = "") -> CheckResult:
    """Kiểm tra kết nối tới MySQL server."""
    host, port = parse_database_host_port(db_url)
    # Thử kết nối socket trước
    if not is_port_in_use(host, port):
        return CheckResult(
            name="MySQL Connection",
            passed=False,
            detail=f"Không thể kết nối socket tới {host}:{port}",
            hint="Chạy 'docker compose up -d' trong thư mục control/ hoặc khởi động dịch vụ MySQL",
        )
    # Thử ping handshake giao thức MySQL
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))
            banner = s.recv(128)
            if banner and len(banner) > 4:
                return CheckResult(
                    name="MySQL Connection",
                    passed=True,
                    detail=f"Kết nối thành công tới MySQL tại {host}:{port}",
                )
    except Exception as exc:
        return CheckResult(
            name="MySQL Connection",
            passed=False,
            detail=f"Lỗi bắt tay MySQL tại {host}:{port}: {exc}",
            hint="Kiểm tra trạng thái container ai-team-mysql qua 'docker compose ps'",
        )
    return CheckResult(
        name="MySQL Connection",
        passed=True,
        detail=f"Cổng MySQL {host}:{port} đang mở",
    )


def check_redis_connection(redis_url: str = "") -> CheckResult:
    """Kiểm tra kết nối tới Redis server."""
    host, port = parse_redis_host_port(redis_url)
    if not is_port_in_use(host, port):
        return CheckResult(
            name="Redis Connection",
            passed=False,
            detail=f"Không thể kết nối socket tới {host}:{port}",
            hint="Chạy 'docker compose up -d' trong thư mục control/ hoặc khởi động Redis",
        )
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))
            s.sendall(b"PING\r\n")
            res = s.recv(32)
            if b"PONG" in res or b"NOAUTH" in res:
                return CheckResult(
                    name="Redis Connection",
                    passed=True,
                    detail=f"Kết nối thành công tới Redis tại {host}:{port} (PING -> PONG)",
                )
    except Exception as exc:
        return CheckResult(
            name="Redis Connection",
            passed=False,
            detail=f"Lỗi phản hồi Redis tại {host}:{port}: {exc}",
            hint="Kiểm tra trạng thái container ai-team-redis",
        )
    return CheckResult(
        name="Redis Connection",
        passed=True,
        detail=f"Cổng Redis {host}:{port} đang mở",
    )


def load_env_variables(env_path: Path | None = None) -> dict[str, str]:
    """Đọc các biến từ tệp .env hoặc control/.env mà không làm lộ secret."""
    paths_to_check: list[Path] = []
    if env_path:
        paths_to_check.append(env_path)
    else:
        root = Path(__file__).resolve().parent.parent
        paths_to_check.extend([root / ".env", root / "control" / ".env", root / "control" / ".env.example"])

    env_vars: dict[str, str] = {}
    for p in paths_to_check:
        if p.exists() and p.is_file():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in env_vars:
                            env_vars[k] = v
            except Exception:
                pass
            if env_vars:
                break
    # Ghi đè bằng biến môi trường thực tế của hệ điều hành nếu có
    for k in list(env_vars.keys()):
        if k in os.environ:
            env_vars[k] = os.environ[k]
    return env_vars


def check_env_keys(env_vars: dict[str, str]) -> CheckResult:
    """Kiểm tra các biến môi trường bắt buộc."""
    required_keys = [
        "APP_ENV",
        "APP_HOST",
        "APP_PORT",
        "DATABASE_URL",
        "REDIS_URL",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_API_KEY",
        "LINEAR_API_KEY",
        "GITHUB_TOKEN",
        "ADMIN_TOKEN",
        "HEARTBEAT_TIMEOUT_SECONDS",
        "MAX_TASKS_PER_RUN",
    ]
    missing = [k for k in required_keys if not env_vars.get(k)]
    if missing:
        return CheckResult(
            name="Environment Keys",
            passed=False,
            detail=f"Thiếu các biến: {', '.join(missing)}",
            hint="Sao chép control/.env.example thành .env và điền đầy đủ các biến",
        )
    return CheckResult(
        name="Environment Keys",
        passed=True,
        detail=f"Đầy đủ {len(required_keys)} biến bắt buộc",
    )


def check_linear_api_online(api_key: str) -> CheckResult:
    """Kiểm tra chỉ-đọc tới Linear GraphQL API."""
    if not api_key or "placeholder" in api_key:
        return CheckResult(
            name="Linear API (Online)",
            passed=False,
            detail="LINEAR_API_KEY chưa được cấu hình hoặc còn để placeholder",
            hint="Lấy API Key từ Linear: Settings > API > Personal API keys",
        )
    import json
    import urllib.request

    url = "https://api.linear.app/graphql"
    req_body = json.dumps({"query": "{ viewer { id name email } }"}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=req_body,
        headers={
            "Content-Type": "application/json",
            "Authorization": api_key,
            "User-Agent": "ai-team-check-env",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if "data" in data and "viewer" in data["data"]:
                viewer = data["data"]["viewer"]
                name = viewer.get("name", "Unknown")
                return CheckResult(
                    name="Linear API (Online)",
                    passed=True,
                    detail=f"Xác thực Linear thành công (Viewer: {name})",
                )
            errors = data.get("errors", [])
            return CheckResult(
                name="Linear API (Online)",
                passed=False,
                detail=f"Linear API trả lỗi: {errors}",
                hint="Kiểm tra lại LINEAR_API_KEY trong .env",
            )
    except Exception as exc:
        return CheckResult(
            name="Linear API (Online)",
            passed=False,
            detail=f"Lỗi kết nối Linear: {exc}",
            hint="Kiểm tra kết nối internet và tính hợp lệ của LINEAR_API_KEY",
        )


def check_github_api_online(token: str) -> CheckResult:
    """Kiểm tra chỉ-đọc tới GitHub REST API."""
    if not token or "placeholder" in token:
        return CheckResult(
            name="GitHub API (Online)",
            passed=False,
            detail="GITHUB_TOKEN chưa được cấu hình hoặc còn để placeholder",
            hint="Tạo GitHub Personal Access Token với quyền repo",
        )
    import json
    import urllib.request

    url = "https://api.github.com/user"
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "ai-team-check-env",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            login = data.get("login", "Unknown")
            return CheckResult(
                name="GitHub API (Online)",
                passed=True,
                detail=f"Xác thực GitHub thành công (User: {login})",
            )
    except Exception as exc:
        return CheckResult(
            name="GitHub API (Online)",
            passed=False,
            detail=f"Lỗi kết nối GitHub: {exc}",
            hint="Kiểm tra GITHUB_TOKEN trong .env",
        )


def run_all_checks(online: bool = False, env_file: Path | None = None) -> list[CheckResult]:
    """Thực thi toàn bộ danh sách kiểm tra."""
    results: list[CheckResult] = []

    # 1. Python version
    results.append(check_python_version())

    # 2. Git CLI
    results.append(check_git_cli())

    # 3. Docker CLI
    results.append(check_docker_cli())

    # 4. Docker Compose
    results.append(check_docker_compose())

    # Đọc cấu hình .env
    env_vars = load_env_variables(env_file)

    # 5. App Port
    try:
        app_port = int(env_vars.get("APP_PORT", "8000"))
    except ValueError:
        app_port = 8000
    results.append(check_app_port(app_port))

    # 6. MySQL
    db_url = env_vars.get("DATABASE_URL", "")
    results.append(check_mysql_connection(db_url))

    # 7. Redis
    redis_url = env_vars.get("REDIS_URL", "")
    results.append(check_redis_connection(redis_url))

    # 8. Environment keys
    results.append(check_env_keys(env_vars))

    # 9 & 10. Online checks nếu có cờ --online
    if online:
        linear_key = env_vars.get("LINEAR_API_KEY", "")
        results.append(check_linear_api_online(linear_key))

        github_token = env_vars.get("GITHUB_TOKEN", "")
        results.append(check_github_api_online(github_token))

    return results


def print_report(results: list[CheckResult]) -> bool:
    """In bảng báo cáo PASS/FAIL và trả về True nếu tất cả đều đạt."""
    all_passed = True
    print("\n" + "=" * 80)
    print(" BÁO CÁO KIỂM TRA MÔI TRƯỜNG CONTROL NODE (PC1)")
    print("=" * 80)
    print(f"{'HẠNG MỤC':<24} | {'TRẠNG THÁI':<10} | {'CHI TIẾT / GỢI Ý KHẮC PHỤC'}")
    print("-" * 80)

    for r in results:
        status_str = "PASS" if r.passed else "FAIL"
        line = f"{r.name:<24} | {status_str:<10} | {r.detail}"
        print(line)
        if not r.passed:
            all_passed = False
            if r.hint:
                print(f"{'':<24} | {'':<10} | -> GỢI Ý: {r.hint}")

    print("-" * 80)
    if all_passed:
        print(">> KẾT LUẬN: TẤT CẢ HẠNG MỤC ĐẠT CHUẨN! Sẵn sàng vận hành Control Node.")
    else:
        failed_count = sum(1 for r in results if not r.passed)
        print(f">> KẾT LUẬN: CÓ {failed_count} HẠNG MỤC CHƯA ĐẠT. Vui lòng khắc phục theo gợi ý trên.")
    print("=" * 80 + "\n")
    return all_passed


def main() -> None:
    """Điểm vào dòng lệnh."""
    setup_console_encoding()
    parser = argparse.ArgumentParser(description="Kiểm tra môi trường Control Node PC1")
    parser.add_argument(
        "--online",
        action="store_true",
        help="Kiểm tra kết nối chỉ-đọc tới Linear API và GitHub API",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Đường dẫn tệp .env cần kiểm tra (mặc định tìm .env hoặc control/.env)",
    )
    args = parser.parse_args()

    results = run_all_checks(online=args.online, env_file=args.env_file)
    success = print_report(results)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
