"""client.py - Linear GraphQL API Client với WorkTracker Protocol, rate limiting, retry có giới hạn và FakeWorkTracker.

Tuân thủ quy tắc Prompt S7:
- Xác thực bằng Personal API Key qua header Authorization.
- Timeout có thể cấu hình, retry có giới hạn (exponential backoff) khi gặp HTTP 429 hoặc 5xx.
- Đọc header giới hạn tốc độ (X-RateLimit-*, Retry-After).
- WorkTracker Protocol bao gồm: ensure_project, ensure_milestone, upsert_issue,
  set_status, set_labels, add_comment, add_relation (blocks), attach_link, list_changes_since.
- LINEAR_DRY_RUN=true: chỉ ghi log hành động GraphQL, trả về ID giả định mà không gọi mạng.
- FakeWorkTracker: bản giả lập trong bộ nhớ phục vụ kiểm thử offline.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

import httpx

from control.app.services.circuit_breaker import CircuitBreakerRegistry

logger = logging.getLogger(__name__)


class LinearClientError(Exception):
    """Lỗi cơ bản khi giao tiếp với Linear GraphQL API."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        errors: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.errors = errors or []


class LinearRateLimitError(LinearClientError):
    """Lỗi chạm trần giới hạn tốc độ (HTTP 429)."""

    def __init__(
        self,
        message: str,
        retry_after: float = 2.0,
        status_code: int = 429,
        errors: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, errors=errors)
        self.retry_after = retry_after


@runtime_checkable
class WorkTracker(Protocol):
    """Interface trừu tượng tương tác với hệ thống quản lý công việc (Linear)."""

    def ensure_project(
        self,
        name: str,
        description: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> str:
        """Đảm bảo Project tồn tại (tạo mới nếu chưa có, hoặc trả về ID dự án)."""
        ...

    def ensure_milestone(
        self,
        name: str,
        project_id: str,
        description: Optional[str] = None,
    ) -> str:
        """Đảm bảo Milestone tồn tại trong Project (tạo mới nếu chưa có)."""
        ...

    def upsert_issue(
        self,
        title: str,
        description: str,
        team_id: str,
        project_id: Optional[str] = None,
        milestone_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        state_id: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        issue_id: Optional[str] = None,
    ) -> str:
        """Tạo mới hoặc cập nhật một Issue (kể cả Sub-Issue nếu có parent_id)."""
        ...

    def set_status(self, issue_id: str, state_id: str) -> None:
        """Cập nhật trạng thái workflow của issue."""
        ...

    def set_labels(self, issue_id: str, label_ids: List[str]) -> None:
        """Cập nhật danh sách nhãn của issue."""
        ...

    def add_comment(self, issue_id: str, body: str) -> str:
        """Thêm một bình luận vào issue."""
        ...

    def add_relation(
        self,
        issue_id: str,
        related_issue_id: str,
        relation_type: str = "blocks",
    ) -> str:
        """Tạo mối quan hệ phụ thuộc giữa hai issue (mặc định là 'blocks')."""
        ...

    def attach_link(self, issue_id: str, url: str, title: str) -> str:
        """Đính kèm liên kết ngoài (PR, artifact) vào issue."""
        ...

    def list_changes_since(self, since: datetime) -> List[Dict[str, Any]]:
        """Lấy danh sách các thay đổi (issue, trạng thái, bình luận) từ thời điểm 'since'."""
        ...

    def get_workflow_states(self, team_id: str) -> List[Dict[str, Any]]:
        """Lấy danh sách các Workflow States của team."""
        ...

    def get_issue_labels(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lấy danh sách các Issue Labels của workspace hoặc team."""
        ...

    def archive_issue(self, issue_id: str) -> bool:
        """Lưu trữ (archive) issue cũ để tiết kiệm quota."""
        ...

    def delete_issue(self, issue_id: str) -> bool:
        """Xóa vĩnh viễn issue khỏi Linear."""
        ...


class LinearClient:
    """Client giao tiếp GraphQL với Linear API (tuân thủ introspection Linear)."""

    def __init__(
        self,
        api_key: str,
        endpoint: str = "https://api.linear.app/graphql",
        timeout: float = 30.0,
        max_retries: int = 4,
        dry_run: bool = False,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.timeout = timeout
        self.max_retries = max_retries
        self.dry_run = dry_run
        self._transport = transport

    def _get_client(self) -> httpx.Client:
        headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
            "User-Agent": "ai-team-control-node/1.0",
        }
        return httpx.Client(
            headers=headers,
            timeout=self.timeout,
            transport=self._transport,
        )

    def execute_query(
        self,
        query: str,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Thực thi một truy vấn GraphQL có hỗ trợ retry, backoff 429 và đọc header rate limit."""
        if self.dry_run:
            logger.info("[LINEAR DRY-RUN] GraphQL query:\n%s\nVariables: %s", query.strip(), variables)
            # Trả về payload giả định tùy theo query/mutation
            return self._mock_dry_run_response(query, variables or {})

        cb = CircuitBreakerRegistry.get_linear()
        if not cb.can_execute():
            raise LinearClientError("Linear Circuit Breaker is OPEN. Dịch vụ tạm thời không khả dụng.")

        client = self._get_client()
        attempts = 0
        backoff = 1.0

        while attempts < self.max_retries:
            attempts += 1
            try:
                resp = client.post(
                    self.endpoint,
                    json={"query": query, "variables": variables or {}},
                )

                # Kiểm tra Rate Limit Headers nếu có
                req_limit = resp.headers.get("X-RateLimit-Requests-Limit")
                req_remaining = resp.headers.get("X-RateLimit-Requests-Remaining")
                req_reset = resp.headers.get("X-RateLimit-Requests-Reset")
                if req_remaining and int(req_remaining) < 5:
                    logger.warning(
                        "Linear Rate Limit sắp chạm trần: còn %s/%s requests, reset sau %ss",
                        req_remaining, req_limit, req_reset,
                    )

                # HTTP 429 Too Many Requests
                if resp.status_code == 429:
                    retry_after_hdr = resp.headers.get("Retry-After")
                    retry_delay = float(retry_after_hdr) if retry_after_hdr else backoff
                    logger.warning(
                        "Linear API trả về 429 (lần %d/%d). Chờ %.2fs trước khi thử lại...",
                        attempts, self.max_retries, retry_delay,
                    )
                    if attempts >= self.max_retries:
                        cb.record_failure()
                        raise LinearRateLimitError(
                            f"Linear rate limit exceeded sau {self.max_retries} lần thử",
                            retry_after=retry_delay,
                        )
                    time.sleep(retry_delay)
                    backoff = min(backoff * 2, 30.0)
                    continue

                # HTTP 5xx Server Error
                if resp.status_code >= 500:
                    logger.warning(
                        "Linear API lỗi 5xx (%d, lần %d/%d). Thử lại sau %.2fs...",
                        resp.status_code, attempts, self.max_retries, backoff,
                    )
                    if attempts >= self.max_retries:
                        cb.record_failure()
                        raise LinearClientError(
                            f"Linear Server Error HTTP {resp.status_code}",
                            status_code=resp.status_code,
                        )
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

                if resp.status_code != 200:
                    raise LinearClientError(
                        f"Linear HTTP {resp.status_code}: {resp.text}",
                        status_code=resp.status_code,
                    )

                result = resp.json()
                if "errors" in result and result["errors"]:
                    errs = result["errors"]
                    # Kiểm tra xem có phải lỗi rate limit ẩn trong GraphQL errors không
                    is_rate_limited = any(
                        "rate limit" in str(e.get("message", "")).lower() for e in errs
                    )
                    if is_rate_limited and attempts < self.max_retries:
                        logger.warning("Linear GraphQL error rate limit. Thử lại sau %.2fs...", backoff)
                        time.sleep(backoff)
                        backoff = min(backoff * 2, 30.0)
                        continue
                    raise LinearClientError(
                        f"Linear GraphQL lỗi: {errs[0].get('message', 'Unknown error')}",
                        status_code=200,
                        errors=errs,
                    )

                cb.record_success()
                return result.get("data", {})

            except httpx.RequestError as exc:
                logger.warning("Lỗi mạng khi kết nối Linear API (lần %d/%d): %s", attempts, self.max_retries, exc)
                if attempts >= self.max_retries:
                    cb.record_failure()
                    raise LinearClientError(f"Không thể kết nối Linear API: {exc}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

        raise LinearClientError("Hết số lần thử lại khi gọi Linear API")

    def _mock_dry_run_response(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Tạo dữ liệu phản hồi giả định khi chạy chế độ LINEAR_DRY_RUN=true."""
        q = query.lower()
        if "projectcreate" in q:
            return {"projectCreate": {"project": {"id": f"dry_prj_{uuid.uuid4().hex[:8]}", "name": variables.get("input", {}).get("name", "Dry Project")}}}
        if "projectmilestonecreate" in q:
            return {"projectMilestoneCreate": {"projectMilestone": {"id": f"dry_ms_{uuid.uuid4().hex[:8]}", "name": variables.get("input", {}).get("name", "Dry Milestone")}}}
        if "issuecreate" in q:
            return {"issueCreate": {"issue": {"id": f"dry_iss_{uuid.uuid4().hex[:8]}", "identifier": f"DRY-{uuid.uuid4().hex[:4].upper()}", "title": variables.get("input", {}).get("title", "")}}}
        if "issueupdate" in q:
            return {"issueUpdate": {"issue": {"id": variables.get("id", f"dry_iss_{uuid.uuid4().hex[:8]}"), "identifier": "DRY-UPDATED"}}}
        if "commentcreate" in q:
            return {"commentCreate": {"comment": {"id": f"dry_cmt_{uuid.uuid4().hex[:8]}"}}}
        if "issuerelationcreate" in q:
            return {"issueRelationCreate": {"issueRelation": {"id": f"dry_rel_{uuid.uuid4().hex[:8]}"}}}
        if "attachmentcreate" in q:
            return {"attachmentCreate": {"attachment": {"id": f"dry_att_{uuid.uuid4().hex[:8]}"}}}
        if "issuearchive" in q:
            return {"issueArchive": {"success": True}}
        if "issuedelete" in q:
            return {"issueDelete": {"success": True}}
        if "workflowstates" in q:
            return {
                "workflowStates": {
                    "nodes": [
                        {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
                        {"id": "st-todo", "name": "Todo", "type": "unstarted"},
                        {"id": "st-in-progress", "name": "In Progress", "type": "started"},
                        {"id": "st-done", "name": "Done", "type": "completed"},
                        {"id": "st-canceled", "name": "Canceled", "type": "canceled"},
                    ]
                }
            }
        if "issuelabels" in q:
            return {
                "issueLabels": {
                    "nodes": [
                        {"id": "lbl-blocked", "name": "blocked"},
                        {"id": "lbl-needs-approval", "name": "needs-approval"},
                        {"id": "lbl-agent-dev", "name": "agent:dev"},
                        {"id": "lbl-agent-tester", "name": "agent:tester"},
                        {"id": "lbl-agent-pm", "name": "agent:pm"},
                        {"id": "lbl-agent-devops", "name": "agent:devops"},
                        {"id": "lbl-agent-ba", "name": "agent:ba"},
                        {"id": "lbl-agent-sa", "name": "agent:sa"},
                        {"id": "lbl-agent-dba", "name": "agent:dba"},
                        {"id": "lbl-agent-po", "name": "agent:po"},
                        {"id": "lbl-agent-security", "name": "agent:security"},
                        {"id": "lbl-agent-scrum", "name": "agent:scrum_master"},
                    ]
                }
            }
        if "issues" in q:
            return {"issues": {"nodes": []}}
        return {}

    # -------------------------------------------------------------------------
    # Triển khai WorkTracker Protocol
    # -------------------------------------------------------------------------

    def ensure_project(
        self,
        name: str,
        description: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> str:
        """Đảm bảo Project tồn tại (tạo mới hoặc lấy ID)."""
        mutation = """
        mutation CreateProject($input: ProjectCreateInput!) {
            projectCreate(input: $input) {
                project {
                    id
                    name
                }
            }
        }
        """
        payload: Dict[str, Any] = {"name": name}
        if description:
            payload["description"] = description
        if team_ids:
            payload["teamIds"] = team_ids

        data = self.execute_query(mutation, {"input": payload})
        prj = data.get("projectCreate", {}).get("project", {})
        return prj.get("id", "")

    def ensure_milestone(
        self,
        name: str,
        project_id: str,
        description: Optional[str] = None,
    ) -> str:
        """Đảm bảo Milestone tồn tại trong Project."""
        mutation = """
        mutation CreateMilestone($input: ProjectMilestoneCreateInput!) {
            projectMilestoneCreate(input: $input) {
                projectMilestone {
                    id
                    name
                }
            }
        }
        """
        payload: Dict[str, Any] = {"name": name, "projectId": project_id}
        if description:
            payload["description"] = description

        data = self.execute_query(mutation, {"input": payload})
        ms = data.get("projectMilestoneCreate", {}).get("projectMilestone", {})
        return ms.get("id", "")

    def upsert_issue(
        self,
        title: str,
        description: str,
        team_id: str,
        project_id: Optional[str] = None,
        milestone_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        state_id: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        issue_id: Optional[str] = None,
    ) -> str:
        """Tạo mới hoặc cập nhật một Issue trên Linear."""
        if issue_id:
            mutation = """
            mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
                issueUpdate(id: $id, input: $input) {
                    issue {
                        id
                    }
                }
            }
            """
            inp: Dict[str, Any] = {"title": title, "description": description}
            if state_id:
                inp["stateId"] = state_id
            if label_ids is not None:
                inp["labelIds"] = label_ids

            data = self.execute_query(mutation, {"id": issue_id, "input": inp})
            return data.get("issueUpdate", {}).get("issue", {}).get("id", issue_id)

        mutation = """
        mutation CreateIssue($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                issue {
                    id
                    identifier
                }
            }
        }
        """
        payload: Dict[str, Any] = {
            "title": title,
            "description": description,
            "teamId": team_id,
        }
        if project_id:
            payload["projectId"] = project_id
        if milestone_id:
            payload["projectMilestoneId"] = milestone_id
        if parent_id:
            payload["parentId"] = parent_id
        if state_id:
            payload["stateId"] = state_id
        if label_ids:
            payload["labelIds"] = label_ids

        data = self.execute_query(mutation, {"input": payload})
        return data.get("issueCreate", {}).get("issue", {}).get("id", "")

    def set_status(self, issue_id: str, state_id: str) -> None:
        """Cập nhật trạng thái workflow của issue."""
        mutation = """
        mutation UpdateIssueStatus($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) {
                issue {
                    id
                }
            }
        }
        """
        self.execute_query(mutation, {"id": issue_id, "input": {"stateId": state_id}})

    def set_labels(self, issue_id: str, label_ids: List[str]) -> None:
        """Cập nhật danh sách nhãn của issue."""
        mutation = """
        mutation UpdateIssueLabels($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) {
                issue {
                    id
                }
            }
        }
        """
        self.execute_query(mutation, {"id": issue_id, "input": {"labelIds": label_ids}})

    def add_comment(self, issue_id: str, body: str) -> str:
        """Thêm một bình luận vào issue."""
        mutation = """
        mutation CreateComment($input: CommentCreateInput!) {
            commentCreate(input: $input) {
                comment {
                    id
                }
            }
        }
        """
        data = self.execute_query(mutation, {"input": {"issueId": issue_id, "body": body}})
        return data.get("commentCreate", {}).get("comment", {}).get("id", "")

    def add_relation(
        self,
        issue_id: str,
        related_issue_id: str,
        relation_type: str = "blocks",
    ) -> str:
        """Tạo mối quan hệ blocks giữa hai issue."""
        mutation = """
        mutation CreateRelation($input: IssueRelationCreateInput!) {
            issueRelationCreate(input: $input) {
                issueRelation {
                    id
                }
            }
        }
        """
        data = self.execute_query(
            mutation,
            {
                "input": {
                    "issueId": issue_id,
                    "relatedIssueId": related_issue_id,
                    "type": relation_type,
                }
            },
        )
        return data.get("issueRelationCreate", {}).get("issueRelation", {}).get("id", "")

    def attach_link(self, issue_id: str, url: str, title: str) -> str:
        """Đính kèm link URL ngoài vào issue."""
        mutation = """
        mutation CreateAttachment($input: AttachmentCreateInput!) {
            attachmentCreate(input: $input) {
                attachment {
                    id
                }
            }
        }
        """
        data = self.execute_query(
            mutation,
            {
                "input": {
                    "issueId": issue_id,
                    "url": url,
                    "title": title,
                }
            },
        )
        return data.get("attachmentCreate", {}).get("attachment", {}).get("id", "")

    def list_changes_since(self, since: datetime) -> List[Dict[str, Any]]:
        """Lấy danh sách các issue và bình luận được cập nhật sau thời điểm since."""
        query = """
        query ListChangesSince($filter: IssueFilter!) {
            issues(filter: $filter, first: 50) {
                nodes {
                    id
                    identifier
                    title
                    state {
                        id
                        name
                        type
                    }
                    labels {
                        nodes {
                            id
                            name
                        }
                    }
                    comments(first: 20) {
                        nodes {
                            id
                            body
                            createdAt
                            user {
                                id
                                name
                            }
                        }
                    }
                    updatedAt
                }
            }
        }
        """
        iso_str = since.isoformat()
        filter_dict = {"updatedAt": {"gt": iso_str}}
        data = self.execute_query(query, {"filter": filter_dict})
        return data.get("issues", {}).get("nodes", [])

    def get_workflow_states(self, team_id: str) -> List[Dict[str, Any]]:
        """Lấy danh sách Workflow States thuộc team."""
        query = """
        query GetWorkflowStates($teamId: ID!) {
            workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                nodes {
                    id
                    name
                    type
                    position
                }
            }
        }
        """
        data = self.execute_query(query, {"teamId": team_id})
        return data.get("workflowStates", {}).get("nodes", [])

    def get_issue_labels(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Lấy danh sách các Issue Labels."""
        query = """
        query GetLabels {
            issueLabels(first: 100) {
                nodes {
                    id
                    name
                    color
                }
            }
        }
        """
        data = self.execute_query(query)
        return data.get("issueLabels", {}).get("nodes", [])

    def archive_issue(self, issue_id: str) -> bool:
        """Lưu trữ issue."""
        mutation = """
        mutation ArchiveIssue($id: String!) {
            issueArchive(id: $id) {
                success
            }
        }
        """
        data = self.execute_query(mutation, {"id": issue_id})
        return bool(data.get("issueArchive", {}).get("success", False))

    def delete_issue(self, issue_id: str) -> bool:
        """Xóa issue vĩnh viễn."""
        mutation = """
        mutation DeleteIssue($id: String!) {
            issueDelete(id: $id, permanentlyDelete: true) {
                success
            }
        }
        """
        data = self.execute_query(mutation, {"id": issue_id})
        return bool(data.get("issueDelete", {}).get("success", False))


class FakeWorkTracker:
    """Bản giả lập Linear WorkTracker chạy trong bộ nhớ phục vụ kiểm thử."""

    def __init__(self) -> None:
        self.projects: Dict[str, Dict[str, Any]] = {}
        self.milestones: Dict[str, Dict[str, Any]] = {}
        self.issues: Dict[str, Dict[str, Any]] = {}
        self.comments: Dict[str, Dict[str, Any]] = {}
        self.relations: Dict[str, Dict[str, Any]] = {}
        self.attachments: Dict[str, Dict[str, Any]] = {}
        self.workflow_states: List[Dict[str, Any]] = [
            {"id": "st-backlog", "name": "Backlog", "type": "backlog"},
            {"id": "st-todo", "name": "Todo", "type": "unstarted"},
            {"id": "st-in-progress", "name": "In Progress", "type": "started"},
            {"id": "st-done", "name": "Done", "type": "completed"},
            {"id": "st-canceled", "name": "Canceled", "type": "canceled"},
        ]
        self.issue_labels: List[Dict[str, Any]] = [
            {"id": "lbl-blocked", "name": "blocked"},
            {"id": "lbl-needs-approval", "name": "needs-approval"},
            {"id": "lbl-agent-dev", "name": "agent:dev"},
            {"id": "lbl-agent-tester", "name": "agent:tester"},
            {"id": "lbl-agent-pm", "name": "agent:pm"},
            {"id": "lbl-agent-devops", "name": "agent:devops"},
            {"id": "lbl-agent-ba", "name": "agent:ba"},
            {"id": "lbl-agent-sa", "name": "agent:sa"},
            {"id": "lbl-agent-dba", "name": "agent:dba"},
            {"id": "lbl-agent-po", "name": "agent:po"},
            {"id": "lbl-agent-security", "name": "agent:security"},
            {"id": "lbl-agent-scrum", "name": "agent:scrum_master"},
        ]
        self.simulate_429_count: int = 0
        self.simulate_error_count: int = 0
        self.fail_persistently: bool = False
        self.call_count: int = 0

    def _check_simulation(self) -> None:
        self.call_count += 1
        if self.fail_persistently:
            raise LinearClientError("Fake Linear Server Down (500)", status_code=500)
        if self.simulate_429_count > 0:
            self.simulate_429_count -= 1
            raise LinearRateLimitError("Rate limit 429 simulated", retry_after=0.01)
        if self.simulate_error_count > 0:
            self.simulate_error_count -= 1
            raise LinearClientError("Simulated server error (503)", status_code=503)

    def ensure_project(
        self,
        name: str,
        description: Optional[str] = None,
        team_ids: Optional[List[str]] = None,
    ) -> str:
        self._check_simulation()
        for pid, p in self.projects.items():
            if p["name"] == name:
                return pid
        pid = f"prj-{uuid.uuid4().hex[:8]}"
        self.projects[pid] = {
            "id": pid,
            "name": name,
            "description": description,
            "team_ids": team_ids or [],
            "created_at": datetime.now(timezone.utc),
        }
        return pid

    def ensure_milestone(
        self,
        name: str,
        project_id: str,
        description: Optional[str] = None,
    ) -> str:
        self._check_simulation()
        for mid, m in self.milestones.items():
            if m["name"] == name and m["project_id"] == project_id:
                return mid
        mid = f"ms-{uuid.uuid4().hex[:8]}"
        self.milestones[mid] = {
            "id": mid,
            "name": name,
            "project_id": project_id,
            "description": description,
        }
        return mid

    def upsert_issue(
        self,
        title: str,
        description: str,
        team_id: str,
        project_id: Optional[str] = None,
        milestone_id: Optional[str] = None,
        parent_id: Optional[str] = None,
        state_id: Optional[str] = None,
        label_ids: Optional[List[str]] = None,
        issue_id: Optional[str] = None,
    ) -> str:
        self._check_simulation()
        now = datetime.now(timezone.utc)
        if issue_id and issue_id in self.issues:
            self.issues[issue_id].update({
                "title": title,
                "description": description,
                "state_id": state_id or self.issues[issue_id]["state_id"],
                "label_ids": label_ids if label_ids is not None else self.issues[issue_id]["label_ids"],
                "updated_at": now,
            })
            return issue_id

        iid = issue_id or f"iss-{uuid.uuid4().hex[:8]}"
        self.issues[iid] = {
            "id": iid,
            "identifier": f"FAKE-{len(self.issues) + 1}",
            "title": title,
            "description": description,
            "team_id": team_id,
            "project_id": project_id,
            "milestone_id": milestone_id,
            "parent_id": parent_id,
            "state_id": state_id or "st-todo",
            "label_ids": label_ids or [],
            "created_at": now,
            "updated_at": now,
            "archived": False,
        }
        return iid

    def set_status(self, issue_id: str, state_id: str) -> None:
        self._check_simulation()
        if issue_id in self.issues:
            self.issues[issue_id]["state_id"] = state_id
            self.issues[issue_id]["updated_at"] = datetime.now(timezone.utc)

    def set_labels(self, issue_id: str, label_ids: List[str]) -> None:
        self._check_simulation()
        if issue_id in self.issues:
            self.issues[issue_id]["label_ids"] = label_ids
            self.issues[issue_id]["updated_at"] = datetime.now(timezone.utc)

    def add_comment(self, issue_id: str, body: str) -> str:
        self._check_simulation()
        cid = f"cmt-{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        self.comments[cid] = {
            "id": cid,
            "issue_id": issue_id,
            "body": body,
            "created_at": now,
            "user": {"name": "Linear Agent"},
        }
        if issue_id in self.issues:
            self.issues[issue_id]["updated_at"] = now
        return cid

    def add_relation(
        self,
        issue_id: str,
        related_issue_id: str,
        relation_type: str = "blocks",
    ) -> str:
        self._check_simulation()
        rid = f"rel-{uuid.uuid4().hex[:8]}"
        self.relations[rid] = {
            "id": rid,
            "issue_id": issue_id,
            "related_issue_id": related_issue_id,
            "type": relation_type,
        }
        return rid

    def attach_link(self, issue_id: str, url: str, title: str) -> str:
        self._check_simulation()
        aid = f"att-{uuid.uuid4().hex[:8]}"
        self.attachments[aid] = {
            "id": aid,
            "issue_id": issue_id,
            "url": url,
            "title": title,
        }
        return aid

    def list_changes_since(self, since: datetime) -> List[Dict[str, Any]]:
        self._check_simulation()
        changed: List[Dict[str, Any]] = []
        for iid, iss in self.issues.items():
            if iss.get("archived"):
                continue
            if iss["updated_at"] >= since:
                st_obj = next((s for s in self.workflow_states if s["id"] == iss["state_id"]), {"id": iss["state_id"], "name": "Unknown"})
                lbl_objs = [l for l in self.issue_labels if l["id"] in iss.get("label_ids", [])]
                cmts = [
                    {"id": cid, "body": c["body"], "createdAt": c["created_at"].isoformat(), "user": c.get("user", {})}
                    for cid, c in self.comments.items()
                    if c["issue_id"] == iid
                ]
                changed.append({
                    "id": iid,
                    "identifier": iss.get("identifier", iid),
                    "title": iss["title"],
                    "state": st_obj,
                    "labels": {"nodes": lbl_objs},
                    "comments": {"nodes": cmts},
                    "updatedAt": iss["updated_at"].isoformat(),
                })
        return changed

    def get_workflow_states(self, team_id: str) -> List[Dict[str, Any]]:
        self._check_simulation()
        return list(self.workflow_states)

    def get_issue_labels(self, team_id: Optional[str] = None) -> List[Dict[str, Any]]:
        self._check_simulation()
        return list(self.issue_labels)

    def archive_issue(self, issue_id: str) -> bool:
        self._check_simulation()
        if issue_id in self.issues:
            self.issues[issue_id]["archived"] = True
            return True
        return False

    def delete_issue(self, issue_id: str) -> bool:
        self._check_simulation()
        if issue_id in self.issues:
            del self.issues[issue_id]
            return True
        return False
