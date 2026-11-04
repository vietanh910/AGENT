#!/usr/bin/env python3
"""scripts/start_worker.py - Khởi động Worker Host trên PC2..PC5 (Prompt S12).

Cách sử dụng:
    python scripts/start_worker.py --config worker/worker.pc2.yaml
    python scripts/start_worker.py --config worker/worker.pc4.yaml --port 8004
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Thêm root dir vào sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

import uvicorn
from worker.host.config import WorkerHostConfig
from worker.host.main import build_worker_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("start_worker")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Khởi động Worker Host (PC2 đến PC5).")
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default="worker.yaml",
        help="Đường dẫn tới file cấu hình YAML (mặc định: worker.yaml).",
    )
    parser.add_argument(
        "--host",
        type=str,
        default=None,
        help="Ghi đè listen host (ví dụ: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        help="Ghi đè listen port (ví dụ: 8000).",
    )
    parser.add_argument(
        "--control-url",
        type=str,
        default=None,
        help="Ghi đè URL kết nối về Control Node PC1.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config_path = Path(args.config)

    # Nếu không tìm thấy tại đường dẫn chỉ định, kiểm tra các thư mục dự phòng
    if not config_path.is_file():
        candidates = [
            ROOT_DIR / args.config,
            ROOT_DIR / "worker" / args.config,
            ROOT_DIR / f"{args.config}.yaml",
            ROOT_DIR / "worker" / f"{args.config}.yaml",
        ]
        for c in candidates:
            if c.is_file():
                config_path = c
                break

    if not config_path.is_file():
        logger.error(f"Không tìm thấy file cấu hình tại '{args.config}' (đã tìm trong: {[str(c) for c in candidates]})")
        sys.exit(1)

    logger.info(f"Nạp cấu hình Worker Host từ: {config_path}")
    cfg = WorkerHostConfig.load_from_yaml(config_path)

    # Ghi đè tham số dòng lệnh nếu được cung cấp
    if args.host:
        cfg.listen_host = args.host
    if args.port:
        cfg.listen_port = args.port
    if args.control_url:
        cfg.control_url = args.control_url

    logger.info(
        f"Khởi động Worker Host '{cfg.worker_id}' (Máy: {cfg.machine_id}) "
        f"lắng nghe tại http://{cfg.listen_host}:{cfg.listen_port} - Control PC1: {cfg.control_url}"
    )
    logger.info(f"Danh sách agent phụ trách: {[f'{a.agent_id} ({a.role})' for a in cfg.agents]}")

    app = build_worker_app(cfg)
    uvicorn.run(app, host=cfg.listen_host, port=cfg.listen_port, log_level="info")


if __name__ == "__main__":
    main()
