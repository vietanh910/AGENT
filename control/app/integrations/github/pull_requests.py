"""Thao tác với GitHub Pull Requests và quy trình Merge an toàn có Policy Guard."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from control.app.database.repositories import AuditRepository
from control.app.integrations.github.client import GitHubClient, GitHubPolicyError

logger = logging.getLogger(__name__)


def list_pull_requests(
    client: GitHubClient,
    owner: str,
    repo: str,
    state: str = "open",
    use_etag: bool = False,
) -> List[Dict[str, Any]]:
    """Liệt kê danh sách Pull Request trong repository."""
    data = client.request(
        method="GET",
        path=f"repos/{owner}/{repo}/pulls",
        params={"state": state},
        use_etag=use_etag,
    )
    return data if isinstance(data, list) else []


def get_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
    use_etag: bool = False,
) -> Dict[str, Any]:
    """Lấy chi tiết một Pull Request."""
    return client.request(
        method="GET",
        path=f"repos/{owner}/{repo}/pulls/{pull_number}",
        use_etag=use_etag,
    )


def create_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str = "main",
    body: str = "",
    session: Optional[Session] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    actor: str = "dev-01",
) -> Dict[str, Any]:
    """Tạo mới một Pull Request trên GitHub (Ghi Audit Log)."""
    payload = {
        "title": title,
        "head": head,
        "base": base,
        "body": body,
    }

    result = client.request(
        method="POST",
        path=f"repos/{owner}/{repo}/pulls",
        json_data=payload,
    )

    pr_number = result.get("number")
    pr_url = result.get("html_url")

    # Ghi nhận Audit Log cho thao tác tạo PR
    if session:
        try:
            AuditRepository.record_audit(
                session=session,
                data={
                    "run_id": run_id,
                    "task_id": task_id,
                    "actor": actor,
                    "action": "github_create_pull_request",
                    "target": f"{owner}/{repo}#PR-{pr_number}",
                    "result": "SUCCESS",
                    "details": {
                        "pr_number": pr_number,
                        "head": head,
                        "base": base,
                        "url": pr_url,
                    },
                },
            )
            session.commit()
        except Exception as e:
            logger.warning(f"Không thể ghi audit log tạo PR: {e}")

    return result


def merge_pull_request(
    client: GitHubClient,
    owner: str,
    repo: str,
    pull_number: int,
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
    merge_method: str = "squash",
    policy_ok: bool = False,
    session: Optional[Session] = None,
    run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    actor: str = "orchestrator",
) -> Dict[str, Any]:
    """Merge Pull Request (Squash) CHỈ KHI tham số policy_ok=True do Orchestrator phê duyệt.

    Quy tắc an toàn:
    - Nếu policy_ok=False: ném GitHubPolicyError và ghi audit từ chối (REJECTED).
    - Nếu policy_ok=True: thực hiện merge qua API GitHub và ghi audit thành công (SUCCESS).
    """
    target_ref = f"{owner}/{repo}#PR-{pull_number}"

    # 1. Kiểm tra chính sách phê duyệt nghiêm ngặt
    if not policy_ok:
        err_msg = (
            f"Từ chối merge PR {target_ref}: policy_ok=False. "
            "Chỉ Orchestrator sau khi các cổng duyệt (CI/CD, Test, Security Gate) đạt chuẩn mới được cấp phép merge."
        )
        logger.error(err_msg)

        if session:
            try:
                AuditRepository.record_audit(
                    session=session,
                    data={
                        "run_id": run_id,
                        "task_id": task_id,
                        "actor": actor,
                        "action": "github_merge_pull_request",
                        "target": target_ref,
                        "result": "REJECTED",
                        "details": {"reason": "policy_ok_is_false", "merge_method": merge_method},
                    },
                )
                session.commit()
            except Exception as e:
                logger.warning(f"Không thể ghi audit log từ chối merge PR: {e}")

        raise GitHubPolicyError(err_msg, status_code=403)

    # 2. Thực hiện gọi API merge sang GitHub
    payload: Dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message

    res = client.request(
        method="PUT",
        path=f"repos/{owner}/{repo}/pulls/{pull_number}/merge",
        json_data=payload,
    )

    # 3. Ghi nhận Audit Log thành công
    if session:
        try:
            AuditRepository.record_audit(
                session=session,
                data={
                    "run_id": run_id,
                    "task_id": task_id,
                    "actor": actor,
                    "action": "github_merge_pull_request",
                    "target": target_ref,
                    "result": "SUCCESS",
                    "details": {
                        "merged": res.get("merged", True),
                        "sha": res.get("sha"),
                        "message": res.get("message"),
                        "merge_method": merge_method,
                    },
                },
            )
            session.commit()
        except Exception as e:
            logger.warning(f"Không thể ghi audit log merge PR: {e}")

    return res
