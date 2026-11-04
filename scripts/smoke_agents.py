#!/usr/bin/env python3
"""scripts/smoke_agents.py - Smoke test kiểm tra mọi pipeline action của toàn bộ 10 Agent (Prompt S12).

Thực hiện:
- Duyệt qua tất cả các vai trò (10 Role) và tất cả pipeline action trong ACTION_MAP.
- Gửi một TaskRequest hợp lệ chuẩn Agent Contract cho từng action.
- Thực thi qua Agent (Mock LLM / Deterministic handler).
- Xác minh tính hợp lệ của TaskResult:
  + Đúng cấu trúc dữ liệu theo Agent Contract (Pydantic validation).
  + Trạng thái kết thúc hợp lệ (COMPLETED).
  + Task_id và Agent_id toàn vẹn.
  + Artifacts đầu ra đúng loại (ArtifactType) quy định trong quy trình.
  + Có checksum SHA-256 và URI hợp lệ.
- Xuất bảng PASS/FAIL ra terminal.

Cách sử dụng:
    python scripts/smoke_agents.py
"""

from __future__ import annotations

import io
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from agent_contract.enums import AgentRole, ArtifactType, TaskStatus
from agent_contract.models import CONTRACT_VERSION, TaskRequest, TaskResult
from agent_contract.registry import ACTION_MAP, AGENT_REGISTRY_DEFAULT
from worker.fake_agent import ACTION_OUTPUT_ARTIFACT_TYPES, FakeAgent

# Thiết lập encoding console Windows
if sys.platform == "win32" and hasattr(sys.stdout, "buffer"):
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

ROLE_TO_AGENT_ID: Dict[AgentRole, str] = {
    AgentRole.PM: "pm-01",
    AgentRole.DEVOPS: "devops-01",
    AgentRole.SA: "sa-01",
    AgentRole.DBA: "dba-01",
    AgentRole.BA: "ba-01",
    AgentRole.PO: "po-01",
    AgentRole.DEV: "dev-01",
    AgentRole.TESTER: "tester-01",
    AgentRole.SECURITY: "security-01",
    AgentRole.SCRUM_MASTER: "scrum_master-01",
}


def build_sample_request(role: AgentRole, action: str, agent_id: str) -> TaskRequest:
    """Tạo một TaskRequest mẫu hợp lệ cho role và action."""
    return TaskRequest(
        task_id=f"smoke-{role.value.lower()}-{action.replace('_', '-')}",
        project_id="PRJ-SMOKE",
        agent_id=agent_id,
        role=role,
        action=action,
        instruction=f"Thực hiện smoke test action '{action}' cho vai trò '{role.value}' với project Todo App.",
        context={
            "project_name": "Sample Todo App",
            "brief": "Xây dựng ứng dụng todo list một trang đơn giản với FastAPI và SQLite.",
            "stage": "smoke_testing",
        },
        timeout_seconds=60,
    )


def validate_result_contract(request: TaskRequest, result: Any) -> Tuple[bool, str]:
    """Kiểm tra TaskResult có tuân thủ 100% Agent Contract không."""
    if not isinstance(result, TaskResult):
        try:
            result = TaskResult.model_validate(result)
        except Exception as e:
            return False, f"Kết quả không parse được thành TaskResult: {e}"

    if result.task_id != request.task_id:
        return False, f"task_id sai lệch: nhận '{result.task_id}', kỳ vọng '{request.task_id}'"

    if result.agent_id != request.agent_id:
        return False, f"agent_id sai lệch: nhận '{result.agent_id}', kỳ vọng '{request.agent_id}'"

    if result.status != TaskStatus.COMPLETED:
        return False, f"Trạng thái không phải COMPLETED: {result.status}"

    if not result.summary:
        return False, "Trường 'summary' bị rỗng"

    # Kiểm tra Artifacts nếu action có sinh artifact
    expected_art_type = ACTION_OUTPUT_ARTIFACT_TYPES.get(request.action)
    if expected_art_type:
        if not result.artifacts:
            return False, f"Action '{request.action}' yêu cầu artifact loại {expected_art_type.value} nhưng kết quả không có artifact nào"
        art = result.artifacts[0]
        if art.type != expected_art_type:
            return False, f"Loại artifact sai lệch: nhận '{art.type}', kỳ vọng '{expected_art_type}'"
        checksum_val = getattr(art, "checksum", None) or getattr(art, "checksum_sha256", None)
        if not checksum_val or len(checksum_val) < 32:
            return False, f"Mã băm checksum SHA-256 không hợp lệ: {checksum_val}"
        if not art.uri:
            return False, "URI của artifact bị rỗng"

    return True, "Hợp lệ"


def run_smoke_tests() -> Tuple[List[Dict[str, Any]], bool]:
    """Chạy toàn bộ smoke tests cho 10 Agent và tất cả Action trong ACTION_MAP."""
    results = []
    all_passed = True

    # Khởi tạo instance agent cho mỗi role
    agents: Dict[AgentRole, Any] = {}
    for role, aid in ROLE_TO_AGENT_ID.items():
        agents[role] = FakeAgent(agent_id=aid, role=role)

    print("=" * 105)
    print(" SMOKE TEST 10 AGENT & MỌI PIPELINE ACTION (Agent Contract v" + CONTRACT_VERSION + ")")
    print("=" * 105)
    print(
        f"| {'ROLE':<13} | {'AGENT ID':<16} | {'ACTION':<24} | "
        f"{'ARTIFACT':<18} | {'DURATION':<9} | {'KẾT QUẢ':<8} |"
    )
    print("|" + "-" * 15 + "|" + "-" * 18 + "|" + "-" * 26 + "|" + "-" * 20 + "|" + "-" * 11 + "|" + "-" * 10 + "|")

    for role, actions_dict in ACTION_MAP.items():
        agent_id = ROLE_TO_AGENT_ID.get(role, f"{role.value.lower()}-01")
        agent_obj = agents[role]

        for action_name in actions_dict.keys():
            req = build_sample_request(role, action_name, agent_id)

            t0 = time.perf_counter()
            try:
                res = agent_obj.handle(req)
                t1 = time.perf_counter()
                duration_ms = int((t1 - t0) * 1000)

                valid, reason = validate_result_contract(req, res)
                passed = valid
            except Exception as e:
                t1 = time.perf_counter()
                duration_ms = int((t1 - t0) * 1000)
                valid = False
                reason = f"Ngoại lệ khi handle: {e}"
                passed = False

            if not passed:
                all_passed = False

            expected_type = ACTION_OUTPUT_ARTIFACT_TYPES.get(action_name)
            art_str = expected_type.value if expected_type else "NONE"
            status_str = "PASS" if passed else "FAIL"

            row = {
                "role": role.value,
                "agent_id": agent_id,
                "action": action_name,
                "artifact_type": art_str,
                "duration_ms": duration_ms,
                "passed": passed,
                "reason": reason,
            }
            results.append(row)

            print(
                f"| {role.value:<13} | {agent_id:<16} | {action_name:<24} | "
                f"{art_str:<18} | {duration_ms:<7}ms | {status_str:<8} |"
            )
            if not passed:
                print(f"  👉 LỖI: {reason}")

    print("=" * 105)
    return results, all_passed


def main() -> int:
    results, all_passed = run_smoke_tests()
    total = len(results)
    passed_count = sum(1 for r in results if r["passed"])
    failed_count = total - passed_count

    print(f"\n[+] Tổng số Actions kiểm thử: {total}")
    print(f"[+] Thành công (PASS): {passed_count}/{total}")
    if failed_count > 0:
        print(f"[!] Thất bại (FAIL): {failed_count}/{total}", file=sys.stderr)
        return 1

    print("[+] KẾT QUẢ: 100% Pipeline Actions của toàn bộ 10 Agent đạt chuẩn Contract.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
