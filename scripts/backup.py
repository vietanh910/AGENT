#!/usr/bin/env python3
"""scripts/backup.py - Sao lưu cơ sở dữ liệu MySQL định kỳ (Prompt S11).

Tính năng:
- Sử dụng mysqldump với tùy chọn --single-transaction, --quick, --routines, --triggers.
- Tự động nhận diện Docker container 'ai-team-mysql' hoặc binary mysqldump trên máy chủ,
  kèm cơ chế sao lưu dự phòng thuần Python (pymysql) nếu không có binary.
- Giữ lại N bản sao lưu gần nhất (mặc định keep=7), xóa các bản cũ hơn.
- Không khóa bảng khi đọc (single-transaction cho InnoDB), không làm gián đoạn hệ thống.
"""

from __future__ import annotations

import argparse
import logging
import io
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
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
logger = logging.getLogger("backup")


def parse_db_url(db_url: str) -> Dict[str, Any]:
    """Phân tách chuỗi kết nối DATABASE_URL."""
    # Xử lý url dạng mysql+pymysql://user:pass@host:port/db?charset=...
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
    """Kiểm tra một command có tồn tại trong PATH không."""
    return shutil.which(cmd) is not None


def is_docker_mysql_running(container_name: str = "ai-team-mysql") -> bool:
    """Kiểm tra Docker container mysql có đang chạy không."""
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


def dump_with_mysqldump(conn_info: Dict[str, Any], output_file: Path) -> bool:
    """Thực thi mysqldump trực tiếp trên máy host."""
    if not is_command_available("mysqldump"):
        return False

    cmd = [
        "mysqldump",
        f"-h{conn_info['host']}",
        f"-P{conn_info['port']}",
        f"-u{conn_info['user']}",
        f"-p{conn_info['password']}",
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        conn_info["database"],
    ]

    try:
        logger.info("Đang thực hiện mysqldump trên host...")
        with open(output_file, "w", encoding="utf-8") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True, check=True)
        logger.info(f"mysqldump hoàn tất: {output_file} ({output_file.stat().st_size} bytes)")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"mysqldump trên host thất bại: {e.stderr}")
        return False


def dump_with_docker(conn_info: Dict[str, Any], output_file: Path, container_name: str = "ai-team-mysql") -> bool:
    """Thực thi mysqldump qua Docker container 'ai-team-mysql'."""
    if not is_docker_mysql_running(container_name):
        return False

    cmd = [
        "docker",
        "exec",
        container_name,
        "mysqldump",
        f"-u{conn_info['user']}",
        f"-p{conn_info['password']}",
        "--single-transaction",
        "--quick",
        "--routines",
        "--triggers",
        conn_info["database"],
    ]

    try:
        logger.info(f"Đang thực hiện mysqldump qua Docker container '{container_name}'...")
        with open(output_file, "w", encoding="utf-8") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, text=True, check=True)
        logger.info(f"Docker mysqldump hoàn tất: {output_file} ({output_file.stat().st_size} bytes)")
        return True
    except subprocess.CalledProcessError as e:
        logger.warning(f"Docker mysqldump thất bại: {e.stderr}")
        return False


def dump_with_python(conn_info: Dict[str, Any], output_file: Path) -> bool:
    """Sao lưu dự phòng thuần Python (pymysql) khi không có binary mysqldump."""
    import pymysql

    logger.info("Đang thực hiện sao lưu dự phòng bằng Python pymysql...")
    try:
        conn = pymysql.connect(
            host=conn_info["host"],
            port=conn_info["port"],
            user=conn_info["user"],
            password=conn_info["password"],
            database=conn_info["database"],
            charset="utf8mb4",
        )

        with conn.cursor() as cursor, open(output_file, "w", encoding="utf-8") as f:
            f.write(f"-- AI Team MySQL Backup (Python Engine)\n")
            f.write(f"-- Database: {conn_info['database']}\n")
            f.write(f"-- Date: {datetime.now(timezone.utc).isoformat()}\n\n")
            f.write("SET FOREIGN_KEY_CHECKS=0;\n\n")

            cursor.execute("SHOW TABLES;")
            tables = [row[0] for row in cursor.fetchall()]

            for tbl in tables:
                # DDL
                cursor.execute(f"SHOW CREATE TABLE `{tbl}`;")
                create_stmt = cursor.fetchone()[1]
                f.write(f"DROP TABLE IF EXISTS `{tbl}`;\n")
                f.write(f"{create_stmt};\n\n")

                # Data
                cursor.execute(f"SELECT * FROM `{tbl}`;")
                rows = cursor.fetchall()
                if rows:
                    cols = [f"`{desc[0]}`" for desc in cursor.description]
                    cols_str = ", ".join(cols)
                    f.write(f"LOCK TABLES `{tbl}` WRITE;\n")
                    for row in rows:
                        vals = []
                        for val in row:
                            if val is None:
                                vals.append("NULL")
                            elif isinstance(val, (int, float)):
                                vals.append(str(val))
                            elif isinstance(val, datetime):
                                vals.append(f"'{val.strftime('%Y-%m-%d %H:%M:%S')}'")
                            elif isinstance(val, (dict, list)):
                                import json
                                escaped = json.dumps(val).replace("\\", "\\\\").replace("'", "\\'")
                                vals.append(f"'{escaped}'")
                            else:
                                escaped = str(val).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
                                vals.append(f"'{escaped}'")
                        vals_str = ", ".join(vals)
                        f.write(f"INSERT INTO `{tbl}` ({cols_str}) VALUES ({vals_str});\n")
                    f.write(f"UNLOCK TABLES;\n\n")

            f.write("SET FOREIGN_KEY_CHECKS=1;\n")

        conn.close()
        logger.info(f"Python backup hoàn tất: {output_file} ({output_file.stat().st_size} bytes)")
        return True
    except Exception as e:
        logger.error(f"Python backup thất bại: {e}")
        return False


def prune_old_backups(backup_dir: Path, keep: int) -> List[Path]:
    """Giữ lại N bản sao lưu gần nhất, xóa các bản cũ hơn."""
    files = sorted(
        [f for f in backup_dir.glob("backup_*.sql") if f.is_file()],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )

    deleted = []
    if len(files) > keep:
        for old_file in files[keep:]:
            try:
                old_file.unlink()
                deleted.append(old_file)
                logger.info(f"Đã dọn dẹp bản backup cũ: {old_file.name}")
            except Exception as e:
                logger.warning(f"Không thể xóa bản backup cũ {old_file}: {e}")

    return deleted


def run_backup(
    db_url: Optional[str] = None,
    backup_dir: Optional[str] = None,
    keep: int = 7,
) -> Path:
    """Hàm chính thực hiện quy trình sao lưu."""
    target_url = db_url or settings.DATABASE_URL
    conn_info = parse_db_url(target_url)

    target_dir = Path(backup_dir or "control/data/backups")
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_file = target_dir / f"backup_{conn_info['database']}_{timestamp}.sql"

    # 1. Thử host mysqldump
    success = dump_with_mysqldump(conn_info, output_file)

    # 2. Thử docker exec mysqldump
    if not success:
        success = dump_with_docker(conn_info, output_file)

    # 3. Thử fallback python
    if not success:
        success = dump_with_python(conn_info, output_file)

    if not success or not output_file.exists() or output_file.stat().st_size == 0:
        if output_file.exists():
            output_file.unlink(missing_ok=True)
        raise RuntimeError("Tất cả các phương thức sao lưu đều thất bại.")

    # 4. Giữ lại N bản sao lưu
    prune_old_backups(target_dir, keep=keep)
    return output_file


if __name__ == "__main__":
    _setup_console_encoding()
    parser = argparse.ArgumentParser(description="AI Team MySQL Database Backup")
    parser.add_argument("--url", default=None, help="Database URL (mặc định lấy từ settings)")
    parser.add_argument("--dir", default="control/data/backups", help="Thư mục chứa file backup")
    parser.add_argument("--keep", type=int, default=7, help="Số lượng bản sao lưu giữ lại")
    parser.add_argument("--list", action="store_true", help="Liệt kê danh sách các bản sao lưu hiện có")

    args = parser.parse_args()

    if args.list:
        target_dir = Path(args.dir).resolve()
        if not target_dir.exists():
            print(f"Thư mục sao lưu không tồn tại: {target_dir}")
            sys.exit(0)
        files = sorted(
            [f for f in target_dir.glob("*.sql") if f.is_file()],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        if not files:
            print(f"Không có bản sao lưu nào trong {target_dir}")
        else:
            print(f"Danh sách bản sao lưu trong {target_dir} ({len(files)} files):")
            for f in files:
                mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                sz_mb = f.stat().st_size / (1024 * 1024)
                print(f"  - {f.name} ({sz_mb:.2f} MB, {mtime})")
        sys.exit(0)

    try:
        path = run_backup(db_url=args.url, backup_dir=args.dir, keep=args.keep)
        print(f"BACKUP_SUCCESS: {path.resolve()}")
        sys.exit(0)
    except Exception as exc:
        logger.error(f"Lỗi sao lưu: {exc}")
        sys.exit(1)
