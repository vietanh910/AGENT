"""test_check_env.py - Kiểm thử toàn diện kịch bản cho scripts/check_env.py bằng mock offline.

Toàn bộ chạy offline, không gọi mạng ngoài, không yêu cầu docker thật.
"""

import io
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import scripts.check_env as check_env


def test_python_version_pass() -> None:
    """Python >= 3.11 trả về PASS."""
    with patch("sys.version_info", (3, 11, 2, "final", 0)):
        res = check_env.check_python_version()
        assert res.passed is True
        assert "3.11.2" in res.detail


def test_python_version_fail() -> None:
    """Python < 3.11 trả về FAIL kèm gợi ý cài đặt."""
    with patch("sys.version_info", (3, 10, 8, "final", 0)):
        res = check_env.check_python_version()
        assert res.passed is False
        assert "không đạt" in res.detail
        assert "python.org" in res.hint


def test_git_cli_pass() -> None:
    """Git có trong PATH và thực thi thành công trả về PASS."""
    with patch("shutil.which", return_value="/usr/bin/git"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="git version 2.40.0\n")
            res = check_env.check_git_cli()
            assert res.passed is True
            assert "git version 2.40.0" in res.detail


def test_git_cli_not_found_fail() -> None:
    """Git không có trong PATH trả về FAIL."""
    with patch("shutil.which", return_value=None):
        res = check_env.check_git_cli()
        assert res.passed is False
        assert "Không tìm thấy" in res.detail
        assert "git-scm.com" in res.hint


def test_docker_cli_pass() -> None:
    """Docker CLI khả dụng trả về PASS."""
    with patch("shutil.which", return_value="/usr/bin/docker"):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="Docker version 26.0.0\n")
            res = check_env.check_docker_cli()
            assert res.passed is True
            assert "Docker version" in res.detail


def test_docker_cli_not_found_fail() -> None:
    """Docker CLI không có trong PATH trả về FAIL."""
    with patch("shutil.which", return_value=None):
        res = check_env.check_docker_cli()
        assert res.passed is False
        assert "Không tìm thấy" in res.detail


def test_docker_compose_pass() -> None:
    """Docker Compose v2 khả dụng trả về PASS."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(stdout="Docker Compose version v2.26.1\n")
        res = check_env.check_docker_compose()
        assert res.passed is True
        assert "v2.26.1" in res.detail


def test_docker_compose_not_found_fail() -> None:
    """Cả docker compose v2 và v1 đều không có trả về FAIL."""
    with patch("subprocess.run", side_effect=Exception("No such command")):
        with patch("shutil.which", return_value=None):
            res = check_env.check_docker_compose()
            assert res.passed is False
            assert "không khả dụng" in res.detail


def test_app_port_free_pass() -> None:
    """Cổng 8000 đang trống trả về PASS."""
    with patch("scripts.check_env.is_port_in_use", return_value=False):
        res = check_env.check_app_port(8000)
        assert res.passed is True
        assert "đang trống" in res.detail


def test_app_port_running_control_pass() -> None:
    """Cổng 8000 đang bận nhưng là Control Node (/health trả 200) trả về PASS."""
    with patch("scripts.check_env.is_port_in_use", return_value=True):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__.return_value = mock_resp
        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = check_env.check_app_port(8000)
            assert res.passed is True
            assert "/health OK" in res.detail


def test_app_port_foreign_app_fail() -> None:
    """Cổng 8000 đang bị ứng dụng lạ chiếm trả về FAIL."""
    with patch("scripts.check_env.is_port_in_use", return_value=True):
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            res = check_env.check_app_port(8000)
            assert res.passed is False
            assert "đang bị chiếm" in res.detail


def test_mysql_connection_pass() -> None:
    """Kết nối MySQL handshake thành công trả về PASS."""
    with patch("scripts.check_env.is_port_in_use", return_value=True):
        mock_socket = MagicMock()
        mock_socket.__enter__.return_value = mock_socket
        mock_socket.recv.return_value = b"\x00\x00\x00\n8.4.0\x00"
        with patch("socket.socket", return_value=mock_socket):
            res = check_env.check_mysql_connection("mysql+pymysql://root:pw@127.0.0.1:3306/db")
            assert res.passed is True
            assert "thành công" in res.detail


def test_mysql_connection_fail() -> None:
    """Không kết nối được socket tới MySQL trả về FAIL kèm gợi ý."""
    with patch("scripts.check_env.is_port_in_use", return_value=False):
        res = check_env.check_mysql_connection("mysql+pymysql://root:pw@127.0.0.1:3306/db")
        assert res.passed is False
        assert "Không thể kết nối" in res.detail
        assert "docker compose up -d" in res.hint


def test_redis_connection_pass() -> None:
    """Redis phản hồi PING -> PONG trả về PASS."""
    with patch("scripts.check_env.is_port_in_use", return_value=True):
        mock_socket = MagicMock()
        mock_socket.__enter__.return_value = mock_socket
        mock_socket.recv.return_value = b"+PONG\r\n"
        with patch("socket.socket", return_value=mock_socket):
            res = check_env.check_redis_connection("redis://127.0.0.1:6379/0")
            assert res.passed is True
            assert "PING -> PONG" in res.detail


def test_redis_connection_fail() -> None:
    """Không kết nối được socket tới Redis trả về FAIL."""
    with patch("scripts.check_env.is_port_in_use", return_value=False):
        res = check_env.check_redis_connection("redis://127.0.0.1:6379/0")
        assert res.passed is False
        assert "Không thể kết nối" in res.detail
        assert "docker compose up -d" in res.hint


def test_env_keys_complete_pass() -> None:
    """Đủ tất cả 13 khóa bắt buộc trong .env trả về PASS."""
    valid_env = {
        "APP_ENV": "development",
        "APP_HOST": "0.0.0.0",
        "APP_PORT": "8000",
        "DATABASE_URL": "mysql+pymysql://root:pw@127.0.0.1:3306/ai_team",
        "REDIS_URL": "redis://127.0.0.1:6379/0",
        "LLM_PROVIDER": "openai",
        "LLM_MODEL": "gpt-4o-mini",
        "LLM_API_KEY": "sk-123456",
        "LINEAR_API_KEY": "lin_api_123456",
        "GITHUB_TOKEN": "ghp_123456",
        "ADMIN_TOKEN": "secret-admin-token",
        "HEARTBEAT_TIMEOUT_SECONDS": "45",
        "MAX_TASKS_PER_RUN": "60",
    }
    res = check_env.check_env_keys(valid_env)
    assert res.passed is True
    assert "Đầy đủ 13 biến" in res.detail


def test_env_keys_missing_fail() -> None:
    """Thiếu khóa bắt buộc trả về FAIL và liệt kê rõ khóa thiếu."""
    incomplete_env = {
        "APP_ENV": "development",
    }
    res = check_env.check_env_keys(incomplete_env)
    assert res.passed is False
    assert "Thiếu các biến:" in res.detail
    assert "DATABASE_URL" in res.detail


def test_linear_api_online_pass() -> None:
    """Mock gọi chỉ-đọc tới Linear GraphQL API thành công."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"data": {"viewer": {"id": "usr-1", "name": "Test Engineer"}}}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = check_env.check_linear_api_online("lin_api_valid_token_123")
        assert res.passed is True
        assert "Xác thực Linear thành công" in res.detail
        assert "Test Engineer" in res.detail


def test_linear_api_online_fail() -> None:
    """Linear API trả lỗi hoặc token sai."""
    with patch("urllib.request.urlopen", side_effect=Exception("HTTP 401 Unauthorized")):
        res = check_env.check_linear_api_online("lin_api_invalid_token")
        assert res.passed is False
        assert "Lỗi kết nối Linear" in res.detail


def test_github_api_online_pass() -> None:
    """Mock gọi chỉ-đọc tới GitHub user API thành công."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b'{"login": "octocat", "id": 1}'
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = check_env.check_github_api_online("ghp_valid_token_123")
        assert res.passed is True
        assert "Xác thực GitHub thành công" in res.detail
        assert "octocat" in res.detail


def test_github_api_online_fail() -> None:
    """GitHub API trả lỗi hoặc token sai."""
    with patch("urllib.request.urlopen", side_effect=Exception("HTTP 401 Bad credentials")):
        res = check_env.check_github_api_online("ghp_invalid_token")
        assert res.passed is False
        assert "Lỗi kết nối GitHub" in res.detail


def test_print_report_all_passed(capsys: pytest.CaptureFixture[str]) -> None:
    """Khi tất cả đều PASS, print_report in kết luận thành công và trả về True."""
    results = [
        check_env.CheckResult("Python", True, "3.11.0"),
        check_env.CheckResult("Git", True, "2.40"),
    ]
    ok = check_env.print_report(results)
    assert ok is True
    captured = capsys.readouterr().out
    assert "TẤT CẢ HẠNG MỤC ĐẠT CHUẨN" in captured


def test_print_report_with_failures(capsys: pytest.CaptureFixture[str]) -> None:
    """Khi có FAIL, print_report in gợi ý và trả về False."""
    results = [
        check_env.CheckResult("Python", True, "3.11.0"),
        check_env.CheckResult("Docker", False, "Missing", hint="Install Docker"),
    ]
    ok = check_env.print_report(results)
    assert ok is False
    captured = capsys.readouterr().out
    assert "CÓ 1 HẠNG MỤC CHƯA ĐẠT" in captured
    assert "Install Docker" in captured
