"""Fake GitHub Client trong bộ nhớ phục vụ kiểm thử offline."""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from sqlalchemy.orm import Session

from control.app.database.repositories import AuditRepository
from control.app.integrations.github.client import GitHubClient, GitHubClientError, GitHubPolicyError


class FakeGitHubClient(GitHubClient):
    """Giả lập GitHub REST API trong bộ nhớ không gọi mạng."""

    def __init__(self, token: str = "fake_github_token") -> None:
        super().__init__(token=token)
        self.repositories: Dict[str, Dict[str, Any]] = {
            "vietanh910/AGENT": {
                "id": 1001,
                "name": "AGENT",
                "full_name": "vietanh910/AGENT",
                "default_branch": "main",
                "private": False,
            }
        }
        self.pull_requests: Dict[str, List[Dict[str, Any]]] = {
            "vietanh910/AGENT": []
        }
        self.check_runs: Dict[str, List[Dict[str, Any]]] = {}
        self.commits: Dict[str, Dict[str, Any]] = {}
        self._next_pr_number = 1

    def list_pull_requests(self, owner: str, repo: str, state: str = "open") -> List[Dict[str, Any]]:
        """Tiện ích trả về danh sách PR cho repo."""
        return self.pull_requests.get(f"{owner}/{repo}", [])

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        use_etag: bool = False,
    ) -> Any:
        path = path.lstrip("/")
        parts = path.split("/")

        # GET repos/{owner}/{repo}
        if method == "GET" and len(parts) == 3 and parts[0] == "repos":
            repo_key = f"{parts[1]}/{parts[2]}"
            if repo_key in self.repositories:
                return self.repositories[repo_key]
            return {
                "id": 9999,
                "name": parts[2],
                "full_name": repo_key,
                "default_branch": "main",
            }

        # GET repos/{owner}/{repo}/commits/{sha}
        if method == "GET" and len(parts) == 5 and parts[3] == "commits":
            sha = parts[4]
            return self.commits.get(sha, {
                "sha": sha,
                "commit": {"message": "Mock commit", "author": {"name": "Agent"}},
            })

        # GET repos/{owner}/{repo}/commits/{ref}/check-runs
        if method == "GET" and len(parts) == 6 and parts[3] == "commits" and parts[5] == "check-runs":
            ref = parts[4]
            runs = self.check_runs.get(ref, [
                {"id": 1, "name": "CI / unit-tests", "status": "completed", "conclusion": "success"}
            ])
            return {"total_count": len(runs), "check_runs": runs}

        # GET repos/{owner}/{repo}/pulls
        if method == "GET" and len(parts) == 4 and parts[3] == "pulls":
            repo_key = f"{parts[1]}/{parts[2]}"
            return self.pull_requests.get(repo_key, [])

        # GET repos/{owner}/{repo}/pulls/{number}
        if method == "GET" and len(parts) == 5 and parts[3] == "pulls":
            repo_key = f"{parts[1]}/{parts[2]}"
            pr_num = int(parts[4])
            prs = self.pull_requests.get(repo_key, [])
            for pr in prs:
                if pr.get("number") == pr_num:
                    return pr
            raise GitHubClientError(f"Không tìm thấy PR #{pr_num}", status_code=404)

        # POST repos/{owner}/{repo}/pulls
        if method == "POST" and len(parts) == 4 and parts[3] == "pulls":
            repo_key = f"{parts[1]}/{parts[2]}"
            data = json_data or {}
            pr_num = self._next_pr_number
            self._next_pr_number += 1
            new_pr = {
                "number": pr_num,
                "title": data.get("title", f"PR #{pr_num}"),
                "body": data.get("body", ""),
                "head": {"ref": data.get("head")},
                "base": {"ref": data.get("base", "main")},
                "state": "open",
                "html_url": f"https://github.com/{repo_key}/pull/{pr_num}",
                "merged": False,
            }
            if repo_key not in self.pull_requests:
                self.pull_requests[repo_key] = []
            self.pull_requests[repo_key].append(new_pr)
            return new_pr

        # PUT repos/{owner}/{repo}/pulls/{number}/merge
        if method == "PUT" and len(parts) == 6 and parts[3] == "pulls" and parts[5] == "merge":
            repo_key = f"{parts[1]}/{parts[2]}"
            pr_num = int(parts[4])
            prs = self.pull_requests.get(repo_key, [])
            for pr in prs:
                if pr.get("number") == pr_num:
                    pr["merged"] = True
                    pr["state"] = "closed"
                    return {
                        "sha": "0123456789abcdef0123456789abcdef01234567",
                        "merged": True,
                        "message": "Pull Request successfully merged",
                    }
            raise GitHubClientError(f"Không tìm thấy PR #{pr_num}", status_code=404)

        return {}
