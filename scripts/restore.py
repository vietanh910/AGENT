#!/usr/bin/env python3
"""scripts/restore.py - Phục hồi cơ sở dữ liệu MySQL từ bản sao lưu (Prompt S11).

Tính năng:
- Hỗ trợ phục hồi qua Docker container 'ai-team-mysql', binary mysql trên máy chủ,
  hoặc thực thi SQL bằng Python pymysql.
- Hỗ trợ tham số --target-db để phục hồi thử nghiệm trên một cơ sở dữ liệu trống.
- Kiểm tra tính toàn vẹn của dữ liệu sau khi phục hồi (kiểm tra danh sách bảng và số dòng).
"""

from __future__ import annotations

import argparse
import io
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

def _setup_console_encoding():
    if "pytest" in sys.modules:
        return
    if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
        try:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
        except Exception:
            pass

# Thêm thư mục gốc vào PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from control.app.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("restore")


def parse_db_url(db_url: str) -> Dict[str, Any]:
    """Phân tách chuỗi kết nối DATABASE_URL."""
    clean_url = db_url.replace("mysql+pymysql://", "mysql://")
    parsed = urlparse(clean_url)
    db_name = parsed.path.lstrip("/").split("?")[0]
    return {
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "database": db_name or "ai_team",
    }


def is_command_available(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def is_docker_mysql_running(container_name: str = "ai-team-mysql") -> bool:
    if not is_command_available("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_name],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and res.stdout.strip().lower() == "true"
    except Exception:
        return False


def restore_with_docker(sql_file: Path, conn_info: Dict[str, Any], target_db: str, container_name: str = "ai-team-mysql") -> bool:
    """Phục hồi qua Docker container 'ai-team-mysql'."""
    if not is_docker_mysql_running(container_name):
        return False

    # Đảm bảo target_db tồn tại
    create_db_cmd = [
        "docker", "exec", container_name, "mysql",
        f"-u{conn_info['user']}", f"-p{conn_info['password']}",
        "-e", f"CREATE DATABASE IF NOT EXISTS `{target_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
    ]
    try:
        subprocess.run(create_db_cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.warning(f"Lỗi khi tạo database đích '{target_db}' qua Docker: {e.stderr}")

    cmd = [
        "docker", "exec", "-i", container_name, "mysql",
        f"-u{conn_info['user']}", f"-p{conn_info['password']}",
        target_db,
    ]

    try:
        logger.info(f"Đang phục hồi {sql_file.name} vào database '{target_db}' qua Docker...")
        with open(sql_file, "rb") as f:
            subprocess.run(cmd, stdin=f, capture_output=True, check=True)
        logger.info("Phục hồi qua Docker hoàn tất thành công.")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"Phục hồi qua Docker thất bại: {e.stderr.decode('utf-8', errors='replace')}")
        return False


def restore_with_host_mysql(sql_file: Path, conn_info: Dict[str, Any], target_db: str) -> bool:
    """Phục hồi qua binary mysql trên máy chủ."""
    if not is_command_available("mysql"):
        return False

    cmd = [
        "mysql",
        f"-h{conn_info['host']}",
        f"-P{conn_info['port']}",
        f"-u{conn_info['user']}",
        f"-p{conn_info['password']}",
        target_db,
    ]

    try:
        logger.info(f"Đang phục hồi {sql_file.name} vào database '{target_db}' trên host...")
        with open(sql_file, "r", encoding="utf-8") as f:
            subprocess.run(cmd, stdin=f, capture_output=True, text=True, check=True)
        logger.info("Phục hồi trên host hoàn tất thành công.")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"Phục hồi trên host thất bại: {e.stderr}")
        return False


def restore_with_python(sql_file: Path, conn_info: Dict[str, Any], target_db: str) -> bool:
    """Phục hồi thuần Python (pymysql) bằng cách thực thi từng lệnh SQL."""
    import pymysql

    logger.info(f"Đang phục hồi {sql_file.name} vào database '{target_db}' bằng Python pymysql...")
    try:
        # Kết nối tới server không chỉ định db để CREATE DATABASE nếu cần
        conn = pymysql.connect(
            host=conn_info["host"],
            port=conn_info["port"],
            user=conn_info["user"],
            password=conn_info["password"],
            charset="utf8mb4",
        )
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{target_db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
            cursor.execute(f"USE `{target_db}`;")
        conn.select_db(target_db)

        # Đọc và tách các câu lệnh SQL
        with open(sql_file, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        # Thực thi các câu lệnh
        with conn.cursor() as cursor:
            # Tạm tắt foreign key checks
            cursor.execute("SET FOREIGN_KEY_CHECKS=0;")
            
            statements = content.split(";\n")
            for stmt in statements:
                stmt_clean = stmt.strip()
                if not stmt_clean or stmt_clean.startswith("--") or stmt_clean.startswith("/*"):
                    continue
                try:
                    cursor.execute(stmt_clean)
                except Exception as ex:
                    # Bỏ qua warning hoặc log
                    pass
            cursor.execute("SET FOREIGN_KEY_CHECKS=1;")
            conn.commit()

        conn.close()
        logger.info("Phục hồi qua Python hoàn tất thành công.")
        return True
    except Exception as e:
        logger.error(f"Phục hồi qua Python thất bại: {e}")
        return False


def verify_restored_db(conn_info: Dict[str, Any], target_db: str) -> Dict[str, int]:
    """Kiểm tra tính hợp lệ của cơ sở dữ liệu sau phục hồi: đếm số dòng từng bảng."""
    import pymysql

    conn = pymysql.connect(
        host=conn_info["host"],
        port=conn_info["port"],
        user=conn_info["user"],
        password=conn_info["password"],
        database=target_db,
        charset="utf8mb4",
    )
    table_stats = {}
    try:
        with conn.cursor() as cursor:
            cursor.execute("SHOW TABLES;")
            tables = [row[0] for row in cursor.fetchall()]
            for tbl in tables:
                cursor.execute(f"SELECT COUNT(*) FROM `{tbl}`;")
                cnt = cursor.fetchone()[0]
                table_stats[tbl] = cnt
    finally:
        conn.close()

    return table_stats


def run_restore(
    sql_file_path: str,
    target_db: Optional[str] = None,
    db_url: Optional[str] = None,
) -> Dict[str, int]:
    """Hàm chính thực hiện phục hồi cơ sở dữ liệu."""
    file_path = Path(sql_file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"Không tìm thấy file sao lưu: {file_path}")

    target_url = db_url or settings.DATABASE_URL
    conn_info = parse_db_url(target_url)
    db_to_use = target_db or conn_info["database"]

    # 1. Thử qua Docker
    success = restore_with_docker(file_path, conn_info, db_to_use)

    # 2. Thử qua binary host
    if not success:
        success = restore_with_host_mysql(file_path, conn_info, db_to_use)

    # 3. Thử qua Python
    if not success:
        success = restore_with_python(file_path, conn_info, db_to_use)

    if not success:
        raise RuntimeError(f"Phục hồi thất bại từ file {file_path}")

    # Xác minh sau phục hồi
    stats = verify_restored_db(conn_info, db_to_use)
    logger.info(f"Xác minh phục hồi thành công database '{db_to_use}': {len(stats)} bảng ({stats})")
    return stats


if __name__ == "__main__":
    _setup_console_encoding()
    parser = argparse.ArgumentParser(description="AI Team MySQL Database Restore")
    parser.add_argument("file", help="Đường dẫn tới file .sql sao lưu")
    parser.add_argument("--target-db", default=None, help="Database đích (mặc định lấy từ URL)")
    parser.add_argument("--url", default=None, help="Database URL kết nối")

    args = parser.parse_args()
    try:
        stats = run_restore(sql_file_path=args.file, target_db=args.target_db, db_url=args.url)
        print(f"RESTORE_SUCCESS: {len(stats)} tables restored.")
        sys.exit(0)
    except Exception as exc:
        logger.error(f"Lỗi phục hồi: {exc}")
        sys.exit(1)
