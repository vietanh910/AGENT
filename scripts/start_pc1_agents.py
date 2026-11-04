#!/usr/bin/env python3
"""scripts/start_pc1_agents.py - Khởi động 2 Agent trên máy PC1 (PM: 8001, DEVOPS: 8002).
"""

from __future__ import annotations

import io
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT_DIR = Path(__file__).resolve().parent.parent

if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass


def is_port_ready(url: str) -> bool:
    try:
        r = httpx.get(url, timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def main():
    print("=" * 70)
    print(" KHỞI ĐỘNG 2 AGENT TRÊN MÁY PC1 (MÁY LEADER)")
    print("=" * 70)

    # 1. PM Agent
    print("[*] Đang khởi động PM Agent (cổng 8001)...")
    pm_cmd = [sys.executable, "-m", "uvicorn", "agents.pm.api:app", "--host", "0.0.0.0", "--port", "8001"]
    pm_proc = subprocess.Popen(
        pm_cmd,
        cwd=str(ROOT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    # 2. DevOps Agent
    print("[*] Đang khởi động DevOps Agent (cổng 8002)...")
    devops_cmd = [sys.executable, "-m", "uvicorn", "agents.devops.api:app", "--host", "0.0.0.0", "--port", "8002"]
    devops_proc = subprocess.Popen(
        devops_cmd,
        cwd=str(ROOT_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
    )

    print("[*] Đang đợi cả 2 Agent sẵn sàng...")
    for _ in range(15):
        pm_ok = is_port_ready("http://127.0.0.1:8001/health")
        devops_ok = is_port_ready("http://127.0.0.1:8002/health")
        if pm_ok and devops_ok:
            print("[+] THÀNH CÔNG: Cả 2 Agent trên PC1 đã sẵn sàng!")
            print("    • PM Agent:     http://127.0.0.1:8001 (PID: %d)" % pm_proc.pid)
            print("    • DevOps Agent: http://127.0.0.1:8002 (PID: %d)" % devops_proc.pid)
            print("=" * 70)
            return 0
        time.sleep(1)

    print("[!] Cảnh báo: Agent mất nhiều thời gian hơn bình thường để khởi động.")
    return 1


if __name__ == "__main__":
    main()
