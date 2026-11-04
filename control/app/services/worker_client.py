"""Giao diện WorkerClient và các hiện thực LocalWorker / RemoteWorker.

Tuân thủ:
- Cùng interface WorkerClient: dispatch(), cancel(), get_status().
- LocalWorker chạy agent nội bộ trên PC1 (pm-01, devops-01) qua ThreadPoolExecutor.
- RemoteWorker gọi qua HTTP LAN tới Worker Host (PC2 đến PC5).
- Orchestrator và Dispatcher không phân biệt giữa LocalWorker và RemoteWorker.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional

import httpx

from agent_contract.enums import AgentRole, ErrorCode, TaskStatus
from agent_contract.models import TaskRequest, TaskResult, failed_result, make_error
from agent_contract.system_models import DispatchAck, DispatchRequest

logger = logging.getLogger(__name__)


class WorkerClientError(Exception):
    """Ngoại lệ khi giao tiếp với Worker."""

    def __init__(self, message: str, code: ErrorCode = ErrorCode.DISPATCH_FAILED, retryable: bool = True) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable


class WorkerClient(ABC):
    """Giao diện trừu tượng WorkerClient kết nối tới Worker Host hoặc Local Worker."""

    @abstractmethod
    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        """Giao task bất đồng bộ cho Worker. Trả về DispatchAck (chấp nhận hoặc từ chối)."""

    @abstractmethod
    async def cancel(self, task_id: str) -> bool:
        """Yêu cầu hủy một task đang chạy."""

    @abstractmethod
    async def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Lấy trạng thái thực thi hiện tại của task."""


class RemoteWorkerClient(WorkerClient):
    """Client gọi qua HTTP LAN tới Worker Host chạy trên các PC khác."""

    def __init__(
        self,
        base_url: str,
        secret: str,
        timeout: float = 10.0,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout
        self._custom_client = http_client

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
        }

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        """Gửi POST {worker}/tasks kèm DispatchRequest và callback_url."""
        url = f"{self.base_url}/tasks"
        headers = self._get_headers()
        payload = request.model_dump(mode="json")

        try:
            if self._custom_client:
                resp = await self._custom_client.post(url, json=payload, headers=headers, timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, json=payload, headers=headers)

            if resp.status_code == 202:
                data = resp.json()
                return DispatchAck(task_id=request.task_id, accepted=True, reason=data.get("reason"))

            if resp.status_code == 409:
                return DispatchAck(task_id=request.task_id, accepted=False, reason="BUSY")

            if resp.status_code == 422:
                return DispatchAck(task_id=request.task_id, accepted=False, reason="INVALID_ROLE_OR_ACTION")

            # Các mã lỗi khác coi như dispatch thất bại
            raise WorkerClientError(
                f"Worker trả về HTTP {resp.status_code}: {resp.text}",
                code=ErrorCode.DISPATCH_FAILED,
                retryable=True,
            )

        except httpx.TimeoutException as e:
            logger.warning(f"Timeout khi dispatch tới {url}: {e}")
            raise WorkerClientError(f"Timeout khi dispatch tới {url}: {e}", code=ErrorCode.TIMEOUT, retryable=True)
        except httpx.RequestError as e:
            logger.warning(f"Lỗi mạng khi dispatch tới {url}: {e}")
            raise WorkerClientError(f"Lỗi mạng kết nối tới {url}: {e}", code=ErrorCode.WORKER_OFFLINE, retryable=True)

    async def cancel(self, task_id: str) -> bool:
        """Gửi POST {worker}/tasks/{task_id}/cancel."""
        url = f"{self.base_url}/tasks/{task_id}/cancel"
        headers = self._get_headers()
        try:
            if self._custom_client:
                resp = await self._custom_client.post(url, headers=headers, timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.post(url, headers=headers)
            return resp.status_code in (200, 202, 204)
        except Exception as e:
            logger.warning(f"Lỗi khi gửi yêu cầu hủy task {task_id} tới {url}: {e}")
            return False

    async def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Gửi GET {worker}/tasks/{task_id}."""
        url = f"{self.base_url}/tasks/{task_id}"
        headers = self._get_headers()
        try:
            if self._custom_client:
                resp = await self._custom_client.get(url, headers=headers, timeout=self.timeout)
            else:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None


class LocalWorkerClient(WorkerClient):
    """Client chạy các Agent nội bộ trên PC1 (pm-01 và devops-01) bằng ThreadPoolExecutor."""

    def __init__(
        self,
        worker_id: str = "worker-pc1-local",
        executor: Optional[ThreadPoolExecutor] = None,
        agent_handlers: Optional[Dict[str, Callable[[TaskRequest], TaskResult]]] = None,
        secret: str = "local_pc1_secret",
    ) -> None:
        self.worker_id = worker_id
        self.executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="local-worker")
        self.agent_handlers = agent_handlers or {}
        self.secret = secret
        self._running_tasks: Dict[str, asyncio.Task] = {}
        self._task_statuses: Dict[str, str] = {}

    def register_agent_handler(self, agent_id: str, handler: Callable[[TaskRequest], TaskResult]) -> None:
        """Đăng ký handler thực thi cho agent nội bộ."""
        self.agent_handlers[agent_id] = handler

    async def dispatch(self, request: DispatchRequest) -> DispatchAck:
        """Giao việc cho agent cục bộ trong thread riêng và gửi kết quả qua callback."""
        agent_id = request.agent_id
        task_id = request.task_id

        # Kiểm tra trùng task_id
        if task_id in self._running_tasks and not self._running_tasks[task_id].done():
            return DispatchAck(task_id=task_id, accepted=True, reason="ALREADY_RUNNING")

        # Kiểm tra handler
        handler = self.agent_handlers.get(agent_id)
        if not handler:
            return DispatchAck(
                task_id=task_id,
                accepted=False,
                reason=f"Chưa cấu hình handler cho local agent '{agent_id}'.",
            )

        # Chạy tác vụ nền qua threadpool
        loop = asyncio.get_running_loop()

        async def _execute_and_callback() -> None:
            self._task_statuses[task_id] = "RUNNING"
            try:
                # Chạy BaseAgent.handle trong thread
                result: TaskResult = await loop.run_in_executor(self.executor, handler, request)
            except Exception as e:
                logger.exception(f"Lỗi khi thực thi local agent {agent_id}: {e}")
                result = failed_result(
                    request,
                    make_error(ErrorCode.TOOL_ERROR, f"Lỗi nội bộ local agent: {e}", retryable=False),
                )

            self._task_statuses[task_id] = result.status.value

            # Gửi kết quả về callback_url
            if request.callback_url:
                try:
                    headers = {
                        "Authorization": f"Bearer {self.secret}",
                        "Content-Type": "application/json",
                    }
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        await client.post(
                            request.callback_url,
                            json=result.model_dump(mode="json"),
                            headers=headers,
                        )
                except Exception as e:
                    logger.warning(f"LocalWorker không gửi được callback về {request.callback_url}: {e}")

        bg_task = asyncio.create_task(_execute_and_callback())
        self._running_tasks[task_id] = bg_task
        self._task_statuses[task_id] = "ASSIGNED"

        return DispatchAck(task_id=task_id, accepted=True)

    async def cancel(self, task_id: str) -> bool:
        """Hủy task nội bộ nếu đang chạy."""
        bg_task = self._running_tasks.get(task_id)
        if bg_task and not bg_task.done():
            bg_task.cancel()
            self._task_statuses[task_id] = "CANCELLED"
            return True
        return False

    async def get_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        status = self._task_statuses.get(task_id)
        if status:
            return {"task_id": task_id, "status": status}
        return None
