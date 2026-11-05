from __future__ import annotations

def print_project_table(state: ProjectState) -> None:
    header = f"{'STT':<4} | {'BƯỚC':<26} | {'ROLE':<8} | {'ACTION/GATE':<24} | {'STATUS':<16} | {'TRY':<4} | {'ARTIFACTS'}"
    sep = "-" * len(header)
    print("\n" + "=" * len(header))
    print(f"DỰ ÁN: {state.project_id} | TRẠNG THÁI: {state.status.value.upper()} | TASKS: {state.tasks_used} | FIX LOOPS: {state.fix_loops_used}")
    if state.waiting_gate:
        print(f"⏳ ĐANG CHỜ DUYỆT CỔNG: {state.waiting_gate} (Lệnh duyệt: python -m cli project approve {state.project_id} --gate {state.waiting_gate})")
    if state.block_reason:
        print(f"⚠️  LÝ DO BỊ CHẶN: {state.block_reason}")
    if state.missing_questions:
        print("❓ CÁC CÂU HỎI LÀM RÕ CẦN NGƯỜI DÙNG:")
        for q in state.missing_questions:
            print(f"   • {q}")
    print("=" * len(header))
    print(header)
    print(sep)

    for idx, (sid, step) in enumerate(state.steps.items(), 1):
        role_disp = step.role or "-"
        act_disp = step.action or (f"[GATE: {step.gate}]" if step.gate else "-")
        stat_disp = step.status.value.upper()
        arts_disp = ", ".join(a.name for a in step.artifacts) if step.artifacts else "-"
        print(f"{idx:<4} | {sid:<26} | {role_disp:<8} | {act_disp:<24} | {stat_disp:<16} | {step.attempts:<4} | {arts_disp}")

    print("=" * len(header) + "\n")

import time
import json
from conductor.state import (
    ProjectState,
    ProjectStatus,
    StepStatus,
    list_all_projects,
    load_project_state,
    save_project_state,
)
import argparse
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

current_dir = Path(__file__).resolve().parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from agent_contract.models import TaskRequest
from agent import PMAgent
from config import PMConfig
from llm import LLMClient


def main() -> None:
    """CLI quản lý và thực thi cho PM Agent."""
    parser = argparse.ArgumentParser(description="CLI chạy PM Agent độc lập")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. Lệnh xem identity
    subparsers.add_parser("identity", help="In thông tin AgentIdentity của PM")

    # 2. Lệnh chạy từ file JSON
    run_parser = subparsers.add_parser("run", help="Thực thi TaskRequest từ file JSON")
    run_parser.add_argument("task_file", type=str, help="Đường dẫn file task JSON")
    run_parser.add_argument("--mock", action="store_true", help="Chạy ở chế độ Mock LLM")

    # 3. Lệnh gửi prompt trực tiếp
    prompt_parser = subparsers.add_parser("prompt", help="Gửi câu lệnh prompt trực tiếp bằng văn bản")
    prompt_parser.add_argument("text", type=str, help="Nội dung câu prompt yêu cầu PM thực hiện")
    prompt_parser.add_argument(
        "--action",
        type=str,
        default="create_plan",
        help="Action cần thực hiện: create_plan (mặc định), analyze_requirement, decompose_tasks, report_status",
    )
    prompt_parser.add_argument("--mock", action="store_true", help="Chạy ở chế độ Mock LLM")

    # 4. Chế độ chat tương tác liên tục
    chat_parser = subparsers.add_parser("chat", help="Mở phiên chat tương tác trực tiếp với PM Agent")
    chat_parser.add_argument("--mock", action="store_true", help="Chạy ở chế độ Mock LLM")

    
    # 5. Lệnh quản lý project (Conductor)
    proj_parser = subparsers.add_parser("project", help="Quản lý và điều phối quy trình phân phối phần mềm (PM Conductor)")
    proj_sub = proj_parser.add_subparsers(dest="proj_command", required=True)

    # start
    p_start = proj_sub.add_parser("start", help="Khởi động dự án phần mềm tự động")
    p_start.add_argument("--brief-file", required=True, type=str, help="Đường dẫn file brief dự án (Markdown/Text)")
    p_start.add_argument("--auto-approve", action="store_true", help="Tự động duyệt qua các cổng kiểm soát")
    p_start.add_argument("--allow-real-deploy", action="store_true", help="Cho phép triển khai thật (context.execute=true)")
    p_start.add_argument("--mock", action="store_true", help="Chạy ở chế độ Mock LLM")
    p_start.add_argument("--module-plan", type=str, help="File JSON containing project_name, overview and ordered modules/tasks")

    # status
    p_status = proj_sub.add_parser("status", help="Xem trạng thái chi tiết của dự án")
    p_status.add_argument("project_id", type=str, help="Mã dự án (project_id)")
    p_status.add_argument("--watch", action="store_true", help="Theo dõi trạng thái liên tục theo thời gian thực")

    # approve
    p_approve = proj_sub.add_parser("approve", help="Phê duyệt thủ công cổng kiểm soát")
    p_approve.add_argument("project_id", type=str, help="Mã dự án (project_id)")
    p_approve.add_argument("--gate", required=True, choices=["APPROVE_PLAN", "approve_scope", "approve_prod", "approve_deploy"], help="Tên cổng kiểm soát")

    # resume
    p_resume = proj_sub.add_parser("resume", help="Tiếp tục chạy dự án từ bước dở dang")
    p_resume.add_argument("project_id", type=str, help="Mã dự án (project_id)")

    # abort
    p_abort = proj_sub.add_parser("abort", help="Hủy bỏ dự án đang chạy")
    p_abort.add_argument("project_id", type=str, help="Mã dự án (project_id)")

    # list
    proj_sub.add_parser("list", help="Liệt kê danh sách các dự án")
    p_change = proj_sub.add_parser("change", help="Chỉnh sửa dự án từ phiên bản đã bàn giao")
    p_change.add_argument("project_id", help="Stable project_id, not run_id")
    change_input = p_change.add_mutually_exclusive_group(required=True)
    change_input.add_argument("--request", help="Mô tả yêu cầu chỉnh sửa")
    change_input.add_argument("--request-file", help="File chứa yêu cầu chỉnh sửa")
    p_change.add_argument("--base-run", help="Kiểm tra phiên bản gốc đang là phiên bản hiện tại")
    p_change.add_argument("--module-plan", help="File JSON kê khai các module bị ảnh hưởng")
    p_history = proj_sub.add_parser("history", help="Xem lịch sử phiên bản đã bàn giao")
    p_history.add_argument("project_id")


    args = parser.parse_args()
    config = PMConfig()

    if args.command == "identity":
        agent = PMAgent(config=config, llm=LLMClient(config=config, mock=True))
        print(agent.get_identity().model_dump_json(indent=2))

    elif args.command == "run":
        task_path = Path(args.task_file)
        if not task_path.exists():
            print(f"Lỗi: Không tìm thấy file task tại '{task_path}'", file=sys.stderr)
            sys.exit(1)

        with open(task_path, "r", encoding="utf-8") as f:
            raw_json = f.read()

        request = TaskRequest.model_validate_json(raw_json)
        use_mock = args.mock or not bool(config.llm_api_key.strip())
        llm = LLMClient(config=config, mock=use_mock)
        agent = PMAgent(config=config, llm=llm)
        result = agent.handle(request)
        print(result.model_dump_json(indent=2))

    elif args.command == "prompt":
        now_tag = datetime.now(timezone.utc).strftime("%H%M%S")
        request = TaskRequest(
            task_id=f"TASK-CLI-{now_tag}",
            project_id="PROJ-CLI",
            agent_id=config.agent_id,
            role=config.agent_role,
            action=args.action,
            instruction=args.text,
            context={"source": "cli_prompt"},
        )
        use_mock = args.mock or not bool(config.llm_api_key.strip())
        llm = LLMClient(config=config, mock=use_mock)
        agent = PMAgent(config=config, llm=llm)

        mode_str = "MOCK LLM" if use_mock else f"REAL LLM ({config.llm_model})"
        print(f"[*] Đang gửi prompt tới PM Agent qua {mode_str}...")
        print(f"[*] Action: {args.action}")
        print(f"[*] Prompt: \"{args.text}\"\n")

        result = agent.handle(request)

        print("=" * 70)
        print(f"KẾT QUẢ THỰC THI: {result.status.value}")
        print(f"Tóm tắt: {result.summary}")
        if result.error:
            print(f"Chi tiết lỗi / lý do: {result.error.message} (Mã: {result.error.code.value})")
            questions = result.error.details.get("questions") if result.error.details else None
            if questions:
                print("Các câu hỏi cần làm rõ:")
                for q in questions:
                    print(f"  ? {q}")
        if result.artifacts:
            print("Artifacts tạo ra:")
            for art in result.artifacts:
                print(f"  + {art.name} ({art.type.value}): {art.uri}")
        if result.next_actions:
            print("Gợi ý bước kế tiếp:")
            for na in result.next_actions:
                print(f"  + {na.suggested_role.value} -> {na.action}: {na.reason}")
        print("=" * 70)
        print("\nJSON Chi tiết:")
        print(result.model_dump_json(indent=2))

    elif args.command == "chat":
        use_mock = args.mock or not bool(config.llm_api_key.strip())
        llm = LLMClient(config=config, mock=use_mock)
        agent = PMAgent(config=config, llm=llm)
        mode_str = "MOCK LLM" if use_mock else f"REAL LLM ({config.llm_model})"
        current_action = "create_plan"

        print("=" * 70)
        print(f"💬 PM AGENT INTERACTIVE CONSOLE [{mode_str}]")
        print("Nhập prompt trực tiếp để PM xử lý. Gõ 'exit' hoặc 'quit' để thoát.")
        print("Lệnh đặc biệt: ':action <tên_action>' để chuyển đổi action.")
        print(f"Action hiện tại: {current_action} (create_plan | analyze_requirement | decompose_tasks | report_status)")
        print("=" * 70)

        counter = 1
        while True:
            try:
                user_input = input(f"\n[{counter}] Bạn > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nĐã thoát chế độ chat.")
                break

            if not user_input:
                continue
            if user_input.lower() in ("exit", "quit", "q"):
                print("Tạm biệt!")
                break
            if user_input.startswith(":action"):
                parts = user_input.split(maxsplit=1)
                if len(parts) > 1 and parts[1].strip():
                    current_action = parts[1].strip()
                    print(f"[*] Đã chuyển action sang: {current_action}")
                else:
                    print(f"[*] Action hiện tại: {current_action}")
                continue

            now_tag = datetime.now(timezone.utc).strftime("%H%M%S")
            request = TaskRequest(
                task_id=f"TASK-CHAT-{now_tag}-{counter}",
                project_id="PROJ-CHAT",
                agent_id=config.agent_id,
                role=config.agent_role,
                action=current_action,
                instruction=user_input,
                context={"source": "cli_chat"},
            )
            print(f"[*] Đang xử lý action '{current_action}' với {mode_str}...")
            result = agent.handle(request)

            print(f"\nPM [{result.status.value}] > {result.summary}")
            if result.error:
                print(f"⚠️  Lý do: {result.error.message}")
                questions = result.error.details.get("questions") if result.error.details else None
                if questions:
                    print("❓ PM cần bạn cung cấp thêm thông tin:")
                    for q in questions:
                        print(f"   • {q}")
            if result.artifacts:
                print("📁 Artifacts vừa tạo:")
                for art in result.artifacts:
                    print(f"   • {art.name} ({art.type.value}): {art.uri}")
            if result.next_actions:
                print("👉 Bước kế tiếp gợi ý:")
                for na in result.next_actions:
                    print(f"   • [{na.suggested_role.value}] {na.action}: {na.reason}")
            counter += 1



    elif args.command == "project":
        if config.pm_project_mode != "managed" and args.proj_command in {"change", "history"}:
            raise ValueError("Change requests and version history require PM_PROJECT_MODE=managed")
        if config.pm_project_mode == "managed":
            from project_gateway import control_request, project_path
            command = args.proj_command
            if command == "change":
                brief = args.request or Path(args.request_file).read_text(encoding="utf-8")
                payload = {"brief": brief}
                if args.base_run:
                    payload["base_run_id"] = args.base_run
                if args.module_plan:
                    payload["module_plan"] = json.loads(Path(args.module_plan).read_text(encoding="utf-8"))
                result = control_request(config, "POST", project_path(args.project_id, "/changes"), payload, resource="projects")
            elif command == "history":
                result = control_request(config, "GET", project_path(args.project_id, "/releases"), resource="projects")
            elif command == "start":
                if args.auto_approve or args.allow_real_deploy:
                    raise ValueError("Managed projects use Control Node approval and deployment settings; omit these legacy flags")
                payload = {"brief": Path(args.brief_file).read_text(encoding="utf-8")}
                if args.module_plan:
                    payload["module_plan"] = json.loads(Path(args.module_plan).read_text(encoding="utf-8"))
                result = control_request(config, "POST", payload=payload)
            elif command == "list":
                result = control_request(config, "GET")
            else:
                suffix = {"status": "", "approve": "/approve", "resume": "/resume", "abort": "/cancel"}[command]
                payload = {"gate": args.gate} if command == "approve" else None
                result = control_request(config, "GET" if command == "status" else "POST",
                                         project_path(args.project_id, suffix), payload)
            print(json.dumps(result, indent=2, ensure_ascii=False))
            if command == "status" and args.watch:
                while result.get("status") not in {"COMPLETED", "FAILED", "CANCELLED", "BLOCKED"}:
                    time.sleep(2)
                    result = control_request(config, "GET", project_path(args.project_id))
                    print(json.dumps(result, indent=2, ensure_ascii=False))
            return
        use_mock = getattr(args, "mock", False) or not bool(config.llm_api_key.strip())
        llm = LLMClient(config=config, mock=use_mock)
        agent = PMAgent(config=config, llm=llm)

        if args.proj_command == "start":
            brief_path = Path(args.brief_file)
            if not brief_path.exists():
                print(f"Lỗi: Không tìm thấy file brief tại '{brief_path}'", file=sys.stderr)
                sys.exit(1)
            brief_content = brief_path.read_text(encoding="utf-8")
            proj_id = f"PROJ-{int(time.time())}"
            print(f"[*] Khởi động dự án mới: {proj_id}")
            state = agent.conductor.init_project(
                project_id=proj_id,
                brief=brief_content,
                auto_approve=args.auto_approve,
            )
            state.allow_real_deploy = args.allow_real_deploy
            save_project_state(state, config.data_dir)
            print(f"[*] Bắt đầu thực thi pipeline software_delivery...")
            final_state = agent.conductor.run_pipeline(state)
            print_project_table(final_state)

        elif args.proj_command == "status":
            state = load_project_state(args.project_id, config.data_dir)
            if not state:
                print(f"Lỗi: Không tìm thấy dự án '{args.project_id}'", file=sys.stderr)
                sys.exit(1)
            print_project_table(state)
            if args.watch:
                print("[*] Đang theo dõi trạng thái dự án (Nhấn Ctrl+C để dừng)...")
                try:
                    while True:
                        time.sleep(2.0)
                        s = load_project_state(args.project_id, config.data_dir)
                        if s:
                            print_project_table(s)
                            if s.status in [ProjectStatus.COMPLETED, ProjectStatus.BLOCKED, ProjectStatus.ABORTED]:
                                print(f"[*] Dự án đã kết thúc ở trạng thái: {s.status.value}")
                                break
                except KeyboardInterrupt:
                    print("\n[*] Dừng theo dõi.")

        elif args.proj_command == "approve":
            state = load_project_state(args.project_id, config.data_dir)
            if not state:
                print(f"Lỗi: Không tìm thấy dự án '{args.project_id}'", file=sys.stderr)
                sys.exit(1)
            print(f"[*] Đang phê duyệt cổng '{args.gate}' cho dự án {args.project_id}...")
            final_state = agent.conductor.approve_gate(project_id=args.project_id, gate_name=args.gate)
            print_project_table(final_state)

        elif args.proj_command == "resume":
            state = load_project_state(args.project_id, config.data_dir)
            if not state:
                print(f"Lỗi: Không tìm thấy dự án '{args.project_id}'", file=sys.stderr)
                sys.exit(1)
            print(f"[*] Tiếp tục chạy dự án {args.project_id}...")
            final_state = agent.conductor.resume_project(project_id=args.project_id)
            print_project_table(final_state)

        elif args.proj_command == "abort":
            state = load_project_state(args.project_id, config.data_dir)
            if not state:
                print(f"Lỗi: Không tìm thấy dự án '{args.project_id}'", file=sys.stderr)
                sys.exit(1)
            state.status = ProjectStatus.ABORTED
            state.block_reason = "Dự án bị hủy bỏ bởi người quản trị qua CLI."
            save_project_state(state, config.data_dir)
            print(f"[*] Đã hủy dự án {args.project_id}.")
            print_project_table(state)

        elif args.proj_command == "list":
            projects = list_all_projects(config.data_dir)
            if not projects:
                print("Chưa có dự án nào được tạo.")
            else:
                print(f"\n{'STT':<4} | {'PROJECT ID':<20} | {'TRẠNG THÁI':<16} | {'BƯỚC HIỆN TẠI':<24} | {'NGÀY TẠO'}")
                print("-" * 80)
                for i, p in enumerate(projects, 1):
                    created = p.created_at.strftime("%Y-%m-%d %H:%M:%S") if p.created_at else "-"
                    cur = p.current_step_id or "-"
                    print(f"{i:<4} | {p.project_id:<20} | {p.status.value:<16} | {cur:<24} | {created}")
                print("-" * 80 + "\n")


if __name__ == "__main__":
    main()
