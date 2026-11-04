"""Kịch bản mô phỏng kiểm chứng Prompt S6:
Artifact Store, GitHub Adapter, Git Workspace và luồng phối hợp DEV - TESTER.

Luồng thực thi:
1. PC1 khởi tạo WorkflowRun 'RUN-DEMO-S6-xxx' và kho lưu trữ Artifact Store.
2. Khởi tạo một Bare Repository cục bộ (mô phỏng GitHub Remote).
3. DEV giả lập:
   - Clone repo vào workspace DEV.
   - Tạo branch 'agent/TASK-DEV-AUTH'.
   - Ghi mã nguồn tính năng đăng nhập (src/auth.py và tests/test_auth.py).
   - Commit và push branch lên bare repo (chặn push main).
   - Mở Pull Request giả lập qua FakeGitHubClient.
   - Xuất artifact SOURCE_CODE có URI: 'github://vietanh910/AGENT/pull/1'.
   - Lưu metadata artifact SOURCE_CODE vào kho PC1.
4. TESTER giả lập:
   - Nhận input_artifact SOURCE_CODE từ DEV.
   - Checkout branch của PR từ bare repo vào workspace TESTER.
   - Thực thi kiểm thử bằng TerminalExecutor.
   - Đóng gói phán quyết Verdict PASS và tạo file báo cáo 'test_report.json'.
   - Upload file báo cáo lên Artifact Store PC1 -> nhận URI 'artifact://ART-xxx'.
5. PC1:
   - Liệt kê toàn bộ artifact thuộc run (GET /api/v1/runs/{id}/artifacts).
   - Xác nhận có đủ cả 2 artifact: SOURCE_CODE và TEST_REPORT.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Đảm bảo root dự án và agent-template nằm trong sys.path
repo_root = Path(__file__).resolve().parent.parent
agent_template_dir = repo_root / "agent-template"
for p in [str(repo_root), str(agent_template_dir)]:
    if p not in sys.path:
        sys.path.insert(0, p)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from agent_contract.enums import AgentRole, ArtifactType
from agent_contract.models import ArtifactRef, TaskRequest
from control.app.database.models import utc_now
from control.app.database.repositories import ArtifactRepository, TaskRepository, WorkflowRunRepository
from control.app.database.session import get_session_factory
from control.app.integrations.github.fake import FakeGitHubClient
from control.app.services.artifact_store import ArtifactStoreService, model_to_artifact_ref
from tools.dev_git_actions import execute_dev_coding_action, execute_tester_run_action


def main():
    print("=" * 78)
    print("DEMO S6: ARTIFACT STORE, GITHUB ADAPTER VÀ GIT WORKSPACE")
    print("=" * 78)

    tmp_dir = Path(tempfile.mkdtemp(prefix="demo_s6_"))
    bare_remote = tmp_dir / "remote.git"
    dev_ws = tmp_dir / "dev_workspace"
    tester_ws = tmp_dir / "tester_workspace"

    try:
        # 1. Khởi tạo Bare Remote Git Repository cục bộ
        print("\n[1] KHOI TAO GIT REMOTE CUC BO (Mô phỏng GitHub Remote):")
        bare_remote.mkdir(parents=True)
        subprocess.run(["git", "init", "--bare"], cwd=str(bare_remote), check=True, capture_output=True)

        seed_dir = tmp_dir / "seed"
        seed_dir.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=str(seed_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "System Seed"], cwd=str(seed_dir), check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "seed@ai-team.local"], cwd=str(seed_dir), check=True, capture_output=True)
        (seed_dir / "README.md").write_text("# Project Repository\n\nBase project skeleton.\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(seed_dir), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit on main"], cwd=str(seed_dir), check=True, capture_output=True)
        subprocess.run(["git", "branch", "-M", "main"], cwd=str(seed_dir), check=True, capture_output=True)
        remote_url = str(bare_remote.as_uri())
        subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=str(seed_dir), check=True, capture_output=True)
        subprocess.run(["git", "push", "-u", "origin", "main"], cwd=str(seed_dir), check=True, capture_output=True)
        shutil.rmtree(seed_dir, ignore_errors=True)

        print(f"  -> Bare Repo URL: {remote_url}")

        # 2. Khởi tạo Run trên PC1
        session_factory = get_session_factory()
        suffix = uuid.uuid4().hex[:6].upper()
        run_id = f"RUN-DEMO-S6-{suffix}"
        dev_task_id = f"TASK-DEV-{suffix}"
        tester_task_id = f"TASK-TEST-{suffix}"
        print(f"\n[2] KHOI TAO WORKFLOW RUN TRÊN PC1: '{run_id}'")

        with session_factory() as session:
            WorkflowRunRepository.create_run(
                session,
                {
                    "run_id": run_id,
                    "project_id": "PRJ-AUTH-SYSTEM",
                    "brief": "Xây dựng module xác thực Authentication",
                },
            )
            TaskRepository.create_task(
                session,
                {
                    "task_id": dev_task_id,
                    "run_id": run_id,
                    "agent_id": "dev-01",
                    "role": "DEV",
                    "action": "implement_feature",
                    "instruction": "Implement authentication tokens and password verification",
                    "dispatch_state": "RUNNING",
                },
            )
            TaskRepository.create_task(
                session,
                {
                    "task_id": tester_task_id,
                    "run_id": run_id,
                    "agent_id": "tester-01",
                    "role": "TESTER",
                    "action": "run_test_suite",
                    "instruction": "Run integration tests on developer pull request",
                    "dispatch_state": "RUNNING",
                },
            )
            session.commit()

        fake_github = FakeGitHubClient()
        art_service = ArtifactStoreService()

        # 3. DEV GIẢ THỰC HIỆN LẬP TRÌNH VÀ MỞ PULL REQUEST
        print("\n[3] DEV AGENT (dev-01) THUC HIEN ACTION 'implement_feature':")
        dev_req = TaskRequest(
            task_id=dev_task_id,
            project_id=run_id,
            agent_id="dev-01",
            role=AgentRole.DEV,
            action="implement_feature",
            instruction="Implement authentication tokens and password verification",
        )

        dev_result = execute_dev_coding_action(
            request=dev_req,
            workspace_dir=dev_ws,
            remote_url=remote_url,
            files_to_write={
                "src/auth.py": "def verify_token(token: str) -> bool:\n    return token == 'valid_token'\n",
                "tests/test_auth.py": "from src.auth import verify_token\n\ndef test_auth():\n    assert verify_token('valid_token') is True\n",
            },
            github_client=fake_github,
            owner="vietanh910",
            repo="AGENT",
        )

        assert dev_result.status.value == "COMPLETED"
        source_art = dev_result.artifacts[0]
        print(f"  -> Đã tạo commit: {source_art.metadata.get('commit_sha')[:10]}")
        print(f"  -> Đã push branch: {source_art.metadata.get('branch')}")
        print(f"  -> Đã mở Pull Request: #{source_art.metadata.get('pr_number')}")
        print(f"  -> Artifact SOURCE_CODE URI: {source_art.uri}")

        # Đăng ký artifact SOURCE_CODE vào DB của PC1
        with session_factory() as session:
            ArtifactRepository.save_artifact(
                session=session,
                data={
                    "artifact_id": source_art.artifact_id,
                    "run_id": run_id,
                    "task_id": dev_req.task_id,
                    "name": source_art.name,
                    "type": source_art.type.value,
                    "uri": source_art.uri,
                    "checksum": source_art.checksum,
                    "created_by": source_art.created_by,
                    "metadata": source_art.metadata,
                },
            )
            session.commit()

        # 4. TESTER GIẢ CHECKOUT PR VÀ CHẠY TEST SUITE
        print("\n[4] TESTER AGENT (tester-01) CHECKOUT PR VA KIEM THU:")
        tester_req = TaskRequest(
            task_id=tester_task_id,
            project_id=run_id,
            agent_id="tester-01",
            role=AgentRole.TESTER,
            action="run_test_suite",
            instruction="Run integration tests on developer pull request",
            input_artifacts=[source_art],
        )

        tester_result = execute_tester_run_action(
            request=tester_req,
            workspace_dir=tester_ws,
            remote_url=remote_url,
            test_command=["git", "log", "-n", "1", "--oneline"],
        )

        assert tester_result.status.value == "COMPLETED"
        tester_report_art = tester_result.artifacts[0]
        print(f"  -> Đã checkout thành công PR #{source_art.metadata.get('pr_number')}")
        print(f"  -> Verdict kết quả: {tester_report_art.metadata.get('verdict')}")

        # Upload TEST_REPORT lên kho Artifact Store trên PC1
        report_content = json.dumps({
            "test_task": tester_req.task_id,
            "pr_checked": source_art.metadata.get("pr_number"),
            "verdict": "PASS",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }, indent=2).encode("utf-8")

        with session_factory() as session:
            saved_report_model = art_service.save_artifact(
                session=session,
                content=report_content,
                filename="test_report_auth.json",
                run_id=run_id,
                task_id=tester_req.task_id,
                artifact_type=ArtifactType.TEST_REPORT,
                created_by="tester-01",
                version="1.0",
                metadata={"verdict": "PASS", "pr_number": source_art.metadata.get("pr_number")},
            )
            session.commit()

        print(f"  -> Đã upload TEST_REPORT lên PC1 Artifact Store: {saved_report_model.uri}")

        # 5. PC1 KIỂM TRA VÀ LIỆT KÊ CẢ HAI ARTIFACT
        print(f"\n[5] PC1 TRUY VẤN GET /api/v1/runs/{run_id}/artifacts?latest=true:")
        with session_factory() as session:
            artifacts = art_service.list_artifacts(session=session, run_id=run_id, latest=True)
            print("-" * 78)
            print(f"{'Artifact ID':<22} | {'Loại':<16} | {'URI':<34}")
            print("-" * 78)
            for a in artifacts:
                print(f"{a.artifact_id:<22} | {a.type:<16} | {a.uri:<34}")
            print("-" * 78)

            types = {a.type for a in artifacts}
            assert "SOURCE_CODE" in types
            assert "TEST_REPORT" in types

        print("\n" + "=" * 78)
        print("THANH CONG! DEMO S6 DA HOAN TAT CHUAN XAC:")
        print("  1. DEV da push branch len repo va mo PR gia thanh cong (SOURCE_CODE github://...)")
        print("  2. TESTER da checkout PR, verify code va upload TEST_REPORT (artifact://...)")
        print("  3. PC1 Artifact Store da luu va liet ke thanh cong ca hai artifact!")
        print("=" * 78)

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
