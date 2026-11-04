"""GitHub API Client với xác thực Bearer token, timeout, backoff khi 429 và ETag caching.

Tuân thủ:
- Xác thực bằng GITHUB_TOKEN (Fine-Grained PAT hoặc App Token).
- Xử lý giới hạn tốc độ (Rate Limit, 429): kiểm tra Retry-After và backoff có giới hạn.
- Hỗ trợ ETag caching cho polling (gửi header If-None-Match, nhận 304 trả về cache).
- Không hard-code URL, ID hay Token.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

import httpx

from control.app.services.circuit_breaker import CircuitBreakerRegistry

logger = logging.getLogger(__name__)


class GitHubClientError(Exception):
    """Ngoại lệ khi gọi GitHub API."""

    def __init__(self, message: str, status_code: Optional[int] = None, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class GitHubPolicyError(GitHubClientError):
    """Ngoại lệ vi phạm chính sách phê duyệt khi thao tác GitHub (ví dụ: merge PR khi policy_ok=False)."""
    pass


class GitHubClient:
    """Client kết nối tới GitHub REST API v3."""

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        timeout: float = 30.0,
        max_retries: int = 3,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.transport = transport

        # Bộ nhớ đệm ETag cho polling: url -> (etag, cached_json)
        self._etag_cache: Dict[str, Tuple[str, Any]] = {}

    def _get_headers(self, etag: Optional[str] = None) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "ai-team-control-node",
        }
        if etag:
            headers["If-None-Match"] = etag
        return headers

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_data: Optional[Dict[str, Any]] = None,
        use_etag: bool = False,
    ) -> Any:
        """Gửi HTTP request tới GitHub với retry và xử lý Rate Limit."""
        url = f"{self.base_url}/{path.lstrip('/')}"
        cb = CircuitBreakerRegistry.get_github()
        if not cb.can_execute():
            raise GitHubClientError("GitHub Circuit Breaker is OPEN. Dịch vụ tạm thời không khả dụng.")

        cached_etag = self._etag_cache.get(url, (None, None))[0] if use_etag else None
        headers = self._get_headers(etag=cached_etag)

        attempt = 0
        backoff_delay = 1.0

        with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
            while attempt <= self.max_retries:
                attempt += 1
                try:
                    resp = client.request(
                        method=method.upper(),
                        url=url,
                        params=params,
                        json=json_data,
                        headers=headers,
                    )

                    # 304 Not Modified -> Trả về dữ liệu trong cache
                    if resp.status_code == 304 and use_etag and url in self._etag_cache:
                        logger.debug(f"GitHub ETag 304 Not Modified cho '{url}'. Dùng cache.")
                        return self._etag_cache[url][1]

                    # 429 Too Many Requests hoặc cạn kiệt rate limit (403 với x-ratelimit-remaining == 0)
                    if resp.status_code == 429 or (resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0"):
                        retry_after_str = resp.headers.get("retry-after")
                        wait_seconds = float(retry_after_str) if retry_after_str else backoff_delay
                        wait_seconds = min(wait_seconds, 30.0)  # Giới hạn trần backoff

                        logger.warning(
                            f"GitHub Rate Limit (HTTP {resp.status_code}). Chờ {wait_seconds:.1f}s trước khi thử lại (lần {attempt}/{self.max_retries})..."
                        )
                        time.sleep(wait_seconds)
                        backoff_delay *= 2.0
                        continue

                    # Kiểm tra lỗi HTTP khác
                    if resp.status_code >= 400:
                        err_text = resp.text
                        try:
                            err_details = resp.json()
                        except Exception:
                            err_details = {"raw": err_text}

                        if resp.status_code >= 500:
                            cb.record_failure()

                        raise GitHubClientError(
                            message=f"GitHub API {method} '{path}' trả về HTTP {resp.status_code}: {err_details.get('message', err_text)}",
                            status_code=resp.status_code,
                            details=err_details,
                        )

                    # Thành công 200..299
                    cb.record_success()
                    data = resp.json() if resp.content else {}
                    new_etag = resp.headers.get("etag")
                    if use_etag and new_etag:
                        self._etag_cache[url] = (new_etag, data)

                    return data

                except (httpx.ConnectError, httpx.TimeoutException) as e:
                    if attempt > self.max_retries:
                        cb.record_failure()
                        raise GitHubClientError(f"Lỗi mạng khi kết nối tới GitHub ({url}): {e}") from e
                    logger.warning(f"Lỗi mạng GitHub ({e}). Thử lại lần {attempt}/{self.max_retries} sau {backoff_delay}s...")
                    time.sleep(backoff_delay)
                    backoff_delay *= 2.0

        raise GitHubClientError(f"Vượt quá số lần thử lại tối đa ({self.max_retries}) tới GitHub ({url})")
