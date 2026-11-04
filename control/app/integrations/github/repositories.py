"""Các hàm thao tác với GitHub Repositories và CI Check Runs."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from control.app.integrations.github.client import GitHubClient


def get_repository(client: GitHubClient, owner: str, repo: str, use_etag: bool = False) -> Dict[str, Any]:
    """Lấy thông tin tổng quan của một GitHub Repository."""
    return client.request(
        method="GET",
        path=f"repos/{owner}/{repo}",
        use_etag=use_etag,
    )


def get_commit(client: GitHubClient, owner: str, repo: str, sha: str) -> Dict[str, Any]:
    """Lấy chi tiết một commit theo mã SHA."""
    return client.request(
        method="GET",
        path=f"repos/{owner}/{repo}/commits/{sha}",
    )


def get_check_runs(
    client: GitHubClient,
    owner: str,
    repo: str,
    ref: str,
    use_etag: bool = False,
) -> List[Dict[str, Any]]:
    """Đọc trạng thái CI (check runs) của một commit hoặc branch/PR ref."""
    data = client.request(
        method="GET",
        path=f"repos/{owner}/{repo}/commits/{ref}/check-runs",
        use_etag=use_etag,
    )
    # GitHub API trả về {"total_count": N, "check_runs": [...]}
    if isinstance(data, dict):
        return data.get("check_runs", [])
    if isinstance(data, list):
        return data
    return []


def get_combined_status(
    client: GitHubClient,
    owner: str,
    repo: str,
    ref: str,
    use_etag: bool = False,
) -> Dict[str, Any]:
    """Lấy trạng thái tổng hợp (Commit Statuses API) cho ref."""
    return client.request(
        method="GET",
        path=f"repos/{owner}/{repo}/commits/{ref}/status",
        use_etag=use_etag,
    )
