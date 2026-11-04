#!/usr/bin/env python3
"""scripts/start_control.py - Khởi động PC1 Control Node (Prompt S12).

Thực hiện:
1. Kiểm tra / khởi động hạ tầng Docker Compose (MySQL 8.4 & Redis 7.4).
2. Kiểm tra kết nối tới cơ sở dữ liệu MySQL và Redis.
3. Chạy kiểm tra / đồng bộ schema bảng cơ sở dữ liệu.
4. Khởi động ứng dụng FastAPI Gateway & Orchestrator trên PC1.

Cách sử dụng:
    python scripts/start_control.py
    python scripts/start_control.py --port 8000 --no-docker
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("start_control")


def is_command_available(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def ensure_docker_infrastructure(compose_file: Path) -> bool:
    """Đảm bảo các container MySQL và Redis đang chạy."""
    if not is_command_available("docker"):
        logger.warning("Không tìm thấy lệnh 'docker'. Bỏ qua tự động bật Docker Compose.")
        return False

    try:
        # Kiểm tra container MySQL
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", "ai-team-mysql"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip().lower() == "true":
            logger.info("Container MySQL 'ai-team-mysql' đã sẵn sàng hoạt động.")
            return True

        # Nếu chưa chạy, bật compose
        if compose_file.is_file():
            logger.info(f"Đang khởi động Docker Compose từ {compose_file}...")
            run_res = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "up", "-d"],
                capture_output=True,
                text=True,
                check=False,
            )
            if run_res.returncode == 0:
                logger.info("Docker Compose đã khởi động thành công các dịch vụ hạ tầng.")
                return True
            else:
                logger.warning(f"Lỗi khởi động Docker Compose: {run_res.stderr}")
    except Exception as e:
        logger.warning(f"Không thể kiểm tra Docker: {e}")

    return False


def wait_for_database(max_wait_seconds: int = 30) -> bool:
    """Chờ kết nối tới MySQL sẵn sàng."""
    from control.app.database.session import get_db_engine
    from sqlalchemy import text

    logger.info("Đang kiểm tra kết nối tới MySQL...")
    engine = get_db_engine()
    start_time = time.time()

    while time.time() - start_time < max_wait_seconds:
        try:
            with engine.connect() as conn:
                res = conn.execute(text("SELECT 1")).scalar()
                if res == 1:
                    logger.info("Kết nối MySQL thành công!")
                    return True
        except Exception:
            time.sleep(1.0)

    logger.error(f"Không thể kết nối tới MySQL sau {max_wait_seconds} giây.")
    return False


def check_and_create_schema() -> bool:
    """Đảm bảo tất cả bảng cơ sở dữ liệu đã được tạo."""
    try:
        from control.app.database.models import Base
        from control.app.database.session import get_db_engine

        engine = get_db_engine()
        Base.metadata.create_all(bind=engine)
        logger.info("Đã đồng bộ cấu trúc bảng cơ sở dữ liệu MySQL thành công.")
        return True
    except Exception as e:
        logger.error(f"Lỗi đồng bộ cấu trúc bảng: {e}")
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Khởi động PC1 Control Node.")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Địa chỉ bind (mặc định: 0.0.0.0).")
    parser.add_argument("--port", "-p", type=int, default=8000, help="Cổng dịch vụ (mặc định: 8000).")
    parser.add_argument("--no-docker", action="store_true", help="Bỏ qua bước kiểm tra Docker Compose.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    compose_path = ROOT_DIR / "control" / "docker-compose.yml"

    logger.info("======================================================================")
    logger.info(" KHỞI ĐỘNG PC1 CONTROL NODE - HỆ THỐNG AI-TEAM")
    logger.info("======================================================================")

    # 1. Hạ tầng Docker Compose
    if not args.no_docker:
        ensure_docker_infrastructure(compose_path)

    # 2. Chờ Database MySQL
    db_ok = wait_for_database(max_wait_seconds=20)
    if not db_ok:
        logger.error("Dừng khởi động do không thể kết nối tới MySQL.")
        sys.exit(1)

    # 3. Đồng bộ schema
    check_and_create_schema()

    # 4. Khởi chạy FastAPI Gateway
    import uvicorn
    from control.app.config import settings

    logger.info(f"PC1 Control Node đang lắng nghe tại http://{args.host}:{args.port}")
    logger.info(f"Môi trường: LOG_LEVEL={settings.LOG_LEVEL}, MAX_CONCURRENT_RUNS={settings.MAX_CONCURRENT_RUNS}")

    uvicorn.run("control.app.main:app", host=args.host, port=args.port, reload=False, log_level="info")


if __name__ == "__main__":
    main()
