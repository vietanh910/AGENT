"""cli.py - Giao diện dòng lệnh điều khiển hệ thống ai-team Control Node (Prompt S8).

Cung cấp các lệnh:
- python -m cli run start "<prompt>" [--project-id PRJ-ID]
- python -m cli run status <run_id>
- python -m cli run plan <run_id>
- python -m cli run approve <run_id> [--gate GATE] [--notes NOTES]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Tái cấu hình UTF-8 cho console Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import httpx

from control.app.config import settings
from control.app.database.models import TaskModel, WorkflowRunModel
from control.app.database.repositories import TaskRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.queue.events import get_event_publisher
from control.app.services.approval import ApprovalService
from control.app.services.plan_service import PlanService

API_BASE = f"http://127.0.0.1:{settings.APP_PORT}"
HEADERS = {
    "Authorization": f"Bearer {settings.ADMIN_TOKEN}",
    "Content-Type": "application/json",
}


def _try_http_post(endpoint: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Thử gọi HTTP tới Control Node Gateway."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{API_BASE}{endpoint}", json=payload, headers=HEADERS)
            if resp.status_code in (200, 201):
                return resp.json()
    except Exception:
        pass
    return None


def _try_http_get(endpoint: str) -> Optional[Any]:
    """Thử gọi HTTP GET tới Control Node Gateway."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(f"{API_BASE}{endpoint}", headers=HEADERS)
            if resp.status_code == 200:
                return resp.json()
    except Exception:
        pass
    return None


# -----------------------------------------------------------------------------
# RUN START
# -----------------------------------------------------------------------------
def cmd_run_start(args: argparse.Namespace) -> int:
    prompt = args.prompt.strip()
    project_id = args.project_id or f"PRJ-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
    print(f"Khởi động Workflow Run mới với prompt: '{prompt}'")
    print(f"Mã dự án (project_id): {project_id}")

    # 1. Thử qua HTTP API Gateway nếu server đang bật
    http_res = _try_http_post("/api/v1/runs", {"prompt": prompt, "project_id": project_id})
    if http_res:
        run_id = http_res.get("run_id")
        status = http_res.get("status")
        print("\n[THÀNH CÔNG - QUA API GATEWAY]")
        print(f"  Run ID:     {run_id}")
        print(f"  Trạng thái: {status}")
        print(f"  Dự án:      {project_id}")
        return 0

    # 2. Thực thi trực tiếp qua tầng dịch vụ (Internal Direct Mode)
    print(">> Gateway HTTP không phản hồi, thực thi trực tiếp qua Control Service...")
    session_factory = get_session_factory()
    run_id = f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{project_id[-4:]}"

    with session_factory() as session:
        run = WorkflowRunRepository.create_run(
            session=session,
            run_data={
                "run_id": run_id,
                "project_id": project_id,
                "brief": prompt,
                "status": "PLANNING",
            },
        )
        session.commit()

        pub = get_event_publisher()
        plan_svc = PlanService(publisher=pub)
        plan = plan_svc.create_project_plan(
            session=session,
            run_id=run_id,
            prompt=prompt,
            project_id=project_id,
        )

        refreshed_run = WorkflowRunRepository.get_run(session, run_id)
        current_status = refreshed_run.status if refreshed_run else "UNKNOWN"

    print("\n[THÀNH CÔNG - TRỰC TIẾP]")
    print(f"  Run ID:     {run_id}")
    print(f"  Trạng thái: {current_status}")
    print(f"  Tổng task:  {len(plan.get('tasks', []))}")
    print(f"  Phê duyệt:  APPROVAL_MODE={'Bật (AWAITING_APPROVAL)' if settings.APPROVAL_MODE else 'Tắt (RUNNING)'}")
    return 0


# -----------------------------------------------------------------------------
# RUN STATUS
# -----------------------------------------------------------------------------
def cmd_run_status(args: argparse.Namespace) -> int:
    run_id = args.run_id.strip()

    # Thử qua HTTP
    http_run = _try_http_get(f"/api/v1/runs/{run_id}")
    http_tasks = _try_http_get(f"/api/v1/runs/{run_id}/tasks")

    if http_run:
        print(f"\nThông tin Workflow Run: {run_id}")
        print(f"  Trạng thái: {http_run.get('status')}")
        print(f"  Dự án:      {http_run.get('project_id')}")
        print(f"  Yêu cầu:    {http_run.get('brief')}")
        print(f"  Tạo lúc:    {http_run.get('created_at')}")

        tasks = http_tasks or []
        print(f"\nDanh sách Tác vụ ({len(tasks)} tasks):")
        print(f"  {'TASK ID':<35} | {'ROLE':<12} | {'ACTION':<24} | {'STATE':<12}")
        print("  " + "-" * 90)
        for t in tasks:
            print(f"  {t.get('task_id', ''):<35} | {t.get('role', ''):<12} | {t.get('action', ''):<24} | {t.get('dispatch_state', ''):<12}")
        return 0

    # Trực tiếp qua DB
    session_factory = get_session_factory()
    with session_factory() as session:
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            print(f"LỖI: Không tìm thấy WorkflowRun '{run_id}'.")
            return 1

        print(f"\nThông tin Workflow Run: {run_id}")
        print(f"  Trạng thái: {run.status}")
        print(f"  Dự án:      {run.project_id}")
        print(f"  Yêu cầu:    {run.brief}")
        print(f"  Tạo lúc:    {run.created_at}")

        tasks = session.query(TaskModel).filter(TaskModel.run_id == run_id).order_by(TaskModel.created_at).all()
        print(f"\nDanh sách Tác vụ ({len(tasks)} tasks):")
        print(f"  {'TASK ID':<35} | {'ROLE':<12} | {'ACTION':<24} | {'STATE':<12}")
        print("  " + "-" * 90)
        for t in tasks:
            print(f"  {t.task_id:<35} | {t.role:<12} | {t.action:<24} | {t.dispatch_state:<12}")

    return 0


# -----------------------------------------------------------------------------
# RUN PLAN
# -----------------------------------------------------------------------------
def cmd_run_plan(args: argparse.Namespace) -> int:
    run_id = args.run_id.strip()

    # Thử qua HTTP
    http_plan = _try_http_get(f"/api/v1/runs/{run_id}/plan")
    if http_plan:
        plan = http_plan.get("plan", {})
        print(f"\nKế hoạch thực thi (Plan) cho Run: {run_id}")
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return 0

    # Trực tiếp qua DB
    session_factory = get_session_factory()
    with session_factory() as session:
        run = WorkflowRunRepository.get_run(session, run_id)
        if not run:
            print(f"LỖI: Không tìm thấy WorkflowRun '{run_id}'.")
            return 1
        print(f"\nKế hoạch thực thi (Plan) cho Run: {run_id}")
        print(json.dumps(run.plan or {}, indent=2, ensure_ascii=False))

    return 0


# -----------------------------------------------------------------------------
# RUN APPROVE
# -----------------------------------------------------------------------------
def cmd_run_approve(args: argparse.Namespace) -> int:
    run_id = args.run_id.strip()
    gate = args.gate
    notes = args.notes or "Phê duyệt qua CLI"

    # Thử qua HTTP
    http_res = _try_http_post(f"/api/v1/runs/{run_id}/approve", {"gate": gate, "notes": notes})
    if http_res:
        print(f"\n[THÀNH CÔNG - QUA API]")
        print(f"  Run ID:     {run_id}")
        print(f"  Gate:       {http_res.get('gate')}")
        print(f"  Trạng thái: {http_res.get('status')} -> {http_res.get('run_status')}")
        return 0

    # Trực tiếp qua DB
    session_factory = get_session_factory()
    with session_factory() as session:
        svc = ApprovalService(publisher=get_event_publisher())
        try:
            res = svc.approve(session, run_id=run_id, gate=gate, decided_by="CLI_USER", notes=notes)
            print(f"\n[THÀNH CÔNG - TRỰC TIẾP]")
            print(f"  Run ID:     {run_id}")
            print(f"  Gate:       {res.get('gate')}")
            print(f"  Trạng thái: {res.get('status')} -> {res.get('run_status')}")
            return 0
        except Exception as exc:
            print(f"LỖI: Phê duyệt thất bại: {exc}")
            return 1


# -----------------------------------------------------------------------------
# MAIN CLI DISPATCHER
# -----------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python -m cli",
        description="ai-team Control Node CLI - Quản trị và điều phối luồng phát triển",
    )
    subparsers = parser.add_subparsers(dest="command", help="Nhóm lệnh chính")

    # Group 'run'
    run_parser = subparsers.add_parser("run", help="Quản lý Workflow Runs")
    run_sub = run_parser.add_subparsers(dest="run_action", help="Hành động trên Run")

    # run start
    start_p = run_sub.add_parser("start", help="Khởi động Workflow Run mới từ prompt")
    start_p.add_argument("prompt", type=str, help="Yêu cầu phát triển phần mềm")
    start_p.add_argument("--project-id", dest="project_id", type=str, default=None, help="Mã định danh dự án")

    # run status
    status_p = run_sub.add_parser("status", help="Xem tiến độ và trạng thái các task")
    status_p.add_argument("run_id", type=str, help="Mã Workflow Run")

    # run plan
    plan_p = run_sub.add_parser("plan", help="Xem cấu trúc kế hoạch DAG chi tiết")
    plan_p.add_argument("run_id", type=str, help="Mã Workflow Run")

    # run approve
    approve_p = run_sub.add_parser("approve", help="Phê duyệt Run hoặc cổng kiểm soát GATE")
    approve_p.add_argument("run_id", type=str, help="Mã Workflow Run")
    approve_p.add_argument("--gate", dest="gate", type=str, default=None, help="Tên cổng kiểm soát")
    approve_p.add_argument("--notes", dest="notes", type=str, default=None, help="Ghi chú phê duyệt")

    args = parser.parse_args()

    if args.command == "run":
        if args.run_action == "start":
            return cmd_run_start(args)
        elif args.run_action == "status":
            return cmd_run_status(args)
        elif args.run_action == "plan":
            return cmd_run_plan(args)
        elif args.run_action == "approve":
            return cmd_run_approve(args)
        else:
            run_parser.print_help()
            return 1
    else:
        parser.print_help()
        return 1


if __name__ == "__main__":
    sys.exit(main())
