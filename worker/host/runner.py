"""Bộ thực thi tác vụ AgentRunner cho Worker Host (worker/host/runner.py).

Tuân thủ:
- Mỗi agent sở hữu một hàng đợi/slot kích thước 1 (tối đa 1 task đồng thời).
- Trước khi chạy: tải toàn bộ input_artifact có URI artifact:// về agent/artifacts/inbox/,
  kiểm tra mã băm SHA-256; nếu lỗi mạng hoặc sai checksum thì trả kết quả BLOCKED (MISSING_CONTEXT).
  Artifact có URI github:// thì bỏ qua, agent tự dùng git.
- Chạy BaseAgent.handle() hoặc FakeAgent.handle() trong một thread riêng biệt.
- Hỗ trợ hủy phối hợp (cooperative cancellation) thông qua threading.Event.
- Chặn ở timeout của task: nếu vượt quá timeout_seconds, tự động ngắt và trả về kết quả TIMEOUT.
- Sau khi chạy: upload mọi artifact trong artifacts/index.json của agent lên Artifact Store trên PC1,
  cập nhật URI artifact://<id> vào TaskResult. Nếu upload lỗi thì kết quả không được coi là hoàn thành (FAILED retryable).
- Ghi kết quả vào SQLite Outbox TRƯỚC khi gửi và kích hoạt gửi callback về PC1.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import hashlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx

from agent_contract.enums import AgentRole, AgentStatus, ErrorCode, TaskStatus
from agent_contract.models import (
    ArtifactRef,
    TaskRequest,
    TaskResult,
    blocked_result,
    failed_result,
    make_error,
)
from agent_contract.system_models import DispatchRequest, HeartbeatAgentStatus
from worker.host.outbox import OutboxService

logger = logging.getLogger(__name__)


def sanitize_filename(name: str) -> str:
    """Làm sạch tên file để ghi vào inbox an toàn."""
    if not name or not name.strip():
        return "artifact.dat"
    cleaned = os.path.basename(name.replace("\\", "/").strip())
    cleaned = re.sub(r"[^\w\.\-]", "_", cleaned)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("._")
    return cleaned or "artifact.dat"


class AgentRunner:
    """Quản lý việc thực thi của một Agent duy nhất trên Worker."""

    def __init__(
        self,
        agent_id: str,
        role: AgentRole,
        agent_instance: Any,
        outbox: OutboxService,
        secret: str,
        control_url: str = "http://127.0.0.1:8000",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.agent_id = agent_id
        self.role = role
        self.agent_instance = agent_instance
        self.outbox = outbox
        self.secret = secret
        self.control_url = control_url.rstrip("/")
        self._http_client = http_client

        self.status: AgentStatus = AgentStatus.IDLE
        self.current_task_id: Optional[str] = None
        self._cancel_event = threading.Event()
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"runner-{agent_id}")

    def is_busy(self) -> bool:
        """Kiểm tra agent có đang bận thực hiện task hay không."""
        with self._lock:
            return self.current_task_id is not None or self.status == AgentStatus.RUNNING

    def cancel(self, task_id: str) -> bool:
        """Kích hoạt cờ hủy phối hợp cho task hiện tại."""
        with self._lock:
            if self.current_task_id == task_id:
                logger.info(f"Kích hoạt cờ hủy cho agent '{self.agent_id}' trên task '{task_id}'.")
                self._cancel_event.set()
                return True
        return False

    def submit(self, request: DispatchRequest) -> bool:
        """Tiếp nhận task vào slot thực thi (hàng đợi kích thước 1)."""
        with self._lock:
            if self.current_task_id is not None or self.status == AgentStatus.RUNNING:
                return False

            self.status = AgentStatus.RUNNING
            self.current_task_id = request.task_id
            self._cancel_event.clear()

        # Thực thi trong thread riêng biệt
        threading.Thread(
            target=self._run_task_thread,
            args=(request,),
            daemon=True,
            name=f"worker-{self.agent_id}-{request.task_id}",
        ).start()

        return True

    def _get_agent_inbox_dir(self) -> Path:
        """Xác định thư mục artifacts/inbox của agent."""
        if hasattr(self.agent_instance, "config") and hasattr(self.agent_instance.config, "artifact_dir"):
            inbox = Path(self.agent_instance.config.artifact_dir) / "inbox"
        elif hasattr(self.agent_instance, "artifact_dir"):
            inbox = Path(self.agent_instance.artifact_dir) / "inbox"
        else:
            inbox = Path(f"worker/data/artifacts/{self.role.value.lower()}/inbox")

        inbox.mkdir(parents=True, exist_ok=True)
        return inbox

    def _get_agent_artifact_dir(self) -> Path:
        """Xác định thư mục artifacts của agent."""
        if hasattr(self.agent_instance, "config") and hasattr(self.agent_instance.config, "artifact_dir"):
            p = Path(self.agent_instance.config.artifact_dir)
        elif hasattr(self.agent_instance, "artifact_dir"):
            p = Path(self.agent_instance.artifact_dir)
        else:
            p = Path(f"worker/data/artifacts/{self.role.value.lower()}")

        p.mkdir(parents=True, exist_ok=True)
        return p

    def _get_effective_control_url(self, request: DispatchRequest) -> str:
        """Lấy URL gốc của Control Node từ callback_url hoặc self.control_url."""
        if request.callback_url and "/api/v1/" in request.callback_url:
            return request.callback_url.split("/api/v1/")[0].rstrip("/")
        return self.control_url

    def _download_input_artifacts(self, request: DispatchRequest) -> Optional[TaskResult]:
        """Tải các input_artifact có URI artifact:// về inbox trước khi chạy."""
        inbox_dir = self._get_agent_inbox_dir()
        effective_control_url = self._get_effective_control_url(request)

        for art in request.input_artifacts:
            # URI github:// thì bỏ qua, agent tự clone/checkout qua git
            if art.uri.startswith("github://"):
                logger.debug(f"Bỏ qua tải artifact GitHub: '{art.uri}' (agent tự dùng git).")
                continue

            # Chỉ tải các artifact://
            if art.uri.startswith("artifact://"):
                art_id = art.uri.replace("artifact://", "")
                download_url = f"{effective_control_url}/api/v1/artifacts/{art_id}/content"
                headers = {"Authorization": f"Bearer {self.secret}"}

                logger.info(f"Đang tải input artifact '{art.name}' (ID: {art_id}) từ {download_url}...")
                try:
                    client = self._http_client or httpx.Client(timeout=30.0)
                    resp = client.get(download_url, headers=headers)
                    if resp.status_code != 200:
                        err_msg = f"Tải artifact '{art.name}' thất bại (HTTP {resp.status_code}): {resp.text}"
                        logger.error(err_msg)
                        return blocked_result(
                            request,
                            make_error(ErrorCode.MISSING_CONTEXT, err_msg, retryable=True),
                            summary=err_msg,
                        )

                    content = resp.content

                    # Kiểm tra checksum SHA-256 đối chiếu cả TaskRequest lẫn Server Header
                    actual_sha = hashlib.sha256(content).hexdigest()
                    server_sha = resp.headers.get("X-Checksum-SHA256")

                    if art.checksum and actual_sha.lower() != art.checksum.strip().lower():
                        err_msg = f"Checksum SHA-256 không khớp với TaskRequest cho artifact '{art.name}'! Dự kiến: {art.checksum}, Thực tế: {actual_sha}"
                        logger.error(err_msg)
                        return blocked_result(
                            request,
                            make_error(ErrorCode.MISSING_CONTEXT, err_msg, retryable=False),
                            summary=err_msg,
                        )

                    if server_sha and actual_sha.lower() != server_sha.strip().lower():
                        err_msg = f"Checksum SHA-256 không khớp với Server Header cho artifact '{art.name}'! Header: {server_sha}, Thực tế: {actual_sha}"
                        logger.error(err_msg)
                        return blocked_result(
                            request,
                            make_error(ErrorCode.MISSING_CONTEXT, err_msg, retryable=False),
                            summary=err_msg,
                        )

                    # Ghi file vào thư mục inbox của agent
                    target_file = inbox_dir / sanitize_filename(art.name)
                    with open(target_file, "wb") as f:
                        f.write(content)
                    logger.info(f"Đã lưu input artifact vào '{target_file}' (size={len(content)} bytes).")

                except Exception as e:
                    err_msg = f"Lỗi kết nối khi tải input artifact '{art.name}': {e}"
                    logger.exception(err_msg)
                    return blocked_result(
                        request,
                        make_error(ErrorCode.MISSING_CONTEXT, err_msg, retryable=True),
                        summary=err_msg,
                    )

        return None

    def _upload_agent_artifacts(
        self,
        request: DispatchRequest,
        result: TaskResult,
    ) -> TaskResult:
        """Tải các artifact mới tạo trong artifacts/index.json lên Artifact Store PC1."""
        artifact_dir = self._get_agent_artifact_dir()
        index_file = artifact_dir / "index.json"
        effective_control_url = self._get_effective_control_url(request)

        # Danh sách artifact cần upload: từ index.json hoặc từ result.artifacts
        artifacts_to_upload: List[Dict[str, Any]] = []

        if index_file.exists():
            try:
                with open(index_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        artifacts_to_upload.extend(data)
            except Exception as e:
                logger.warning(f"Không thể đọc file {index_file}: {e}")

        # Nếu index.json rỗng nhưng trong result.artifacts có file cục bộ
        if not artifacts_to_upload and result.artifacts:
            for art in result.artifacts:
                if not art.uri.startswith("artifact://") and not art.uri.startswith("github://"):
                    artifacts_to_upload.append(art.model_dump())

        if not artifacts_to_upload:
            return result

        uploaded_refs: List[ArtifactRef] = []
        client = self._http_client or httpx.Client(timeout=60.0)

        for item in artifacts_to_upload:
            name = item.get("name") or "artifact.dat"
            uri_val = item.get("uri") or name

            # Nếu artifact đã là uri artifact:// hoặc github:// thì giữ nguyên
            if uri_val.startswith("artifact://") or uri_val.startswith("github://"):
                continue

            file_path = artifact_dir / uri_val
            if not file_path.exists():
                file_path = artifact_dir / name
            if not file_path.exists():
                logger.warning(f"Không tìm thấy file vật lý cho artifact '{name}' tại {file_path}. Bỏ qua upload.")
                continue

            try:
                with open(file_path, "rb") as f:
                    file_bytes = f.read()

                checksum = hashlib.sha256(file_bytes).hexdigest()
                upload_meta = {
                    "artifact_id": item.get("artifact_id"),
                    "name": name,
                    "type": item.get("type", "OTHER"),
                    "run_id": request.project_id,
                    "task_id": request.task_id,
                    "version": item.get("version", "1.0"),
                    "checksum": checksum,
                    "created_by": self.agent_id,
                    "metadata": item.get("metadata", {}),
                }

                upload_url = f"{effective_control_url}/api/v1/artifacts"
                headers = {"Authorization": f"Bearer {self.secret}"}
                files = {"file": (name, file_bytes, "application/octet-stream")}
                data = {"metadata": json.dumps(upload_meta)}

                logger.info(f"Đang upload artifact '{name}' lên {upload_url}...")
                resp = client.post(upload_url, headers=headers, files=files, data=data)

                if resp.status_code not in (200, 201):
                    err_msg = f"Upload artifact '{name}' thất bại (HTTP {resp.status_code}): {resp.text}"
                    logger.error(err_msg)
                    # Upload lỗi thì kết quả không được coi là hoàn thành
                    return failed_result(
                        request,
                        make_error(ErrorCode.ARTIFACT_ERROR, err_msg, retryable=True),
                        summary=f"Lỗi upload artifact: {err_msg}",
                    )

                res_data = resp.json()
                uploaded_art_ref = ArtifactRef.model_validate(res_data)
                uploaded_refs.append(uploaded_art_ref)
                logger.info(f"Upload artifact '{name}' thành công -> URI: {uploaded_art_ref.uri}")

            except Exception as e:
                err_msg = f"Ngoại lệ khi upload artifact '{name}': {e}"
                logger.exception(err_msg)
                return failed_result(
                    request,
                    make_error(ErrorCode.ARTIFACT_ERROR, err_msg, retryable=True),
                    summary=f"Lỗi upload artifact: {err_msg}",
                )

        # Cập nhật lại danh sách artifacts trong TaskResult
        if uploaded_refs:
            updated_artifacts: List[ArtifactRef] = []
            uploaded_by_name = {a.name: a for a in uploaded_refs}

            for existing_art in result.artifacts:
                if existing_art.name in uploaded_by_name:
                    updated_artifacts.append(uploaded_by_name[existing_art.name])
                else:
                    updated_artifacts.append(existing_art)

            for u_art in uploaded_refs:
                if not any(a.artifact_id == u_art.artifact_id for a in updated_artifacts):
                    updated_artifacts.append(u_art)

            result.artifacts = updated_artifacts

        return result

    def _run_task_thread(self, request: DispatchRequest) -> None:
        """Vòng lặp thực thi task trong thread độc lập."""
        task_id = request.task_id
        timeout_seconds = request.constraints.timeout_seconds if request.constraints else 1000
        logger.info(f"Agent '{self.agent_id}' bắt đầu xử lý task '{task_id}' (action={request.action}, timeout={timeout_seconds}s).")

        result: Optional[TaskResult] = None

        # 1. TRƯỚC KHI CHẠY: Tải các input_artifact có URI artifact:// về inbox
        blocked_res = self._download_input_artifacts(request)
        if blocked_res is not None:
            result = blocked_res
        else:
            # 2. THỰC THI AGENT TRONG THREADPOOL
            def _target_exec() -> TaskResult:
                try:
                    import inspect
                    sig = inspect.signature(self.agent_instance.handle)
                    if "cancel_event" in sig.parameters:
                        return self.agent_instance.handle(request, cancel_event=self._cancel_event)
                    return self.agent_instance.handle(request)
                except Exception as e:
                    logger.exception(f"Lỗi ngoại lệ trong execution của agent '{self.agent_id}': {e}")
                    return failed_result(
                        request,
                        make_error(ErrorCode.TOOL_ERROR, f"Agent gặp lỗi ngoại lệ: {e}", retryable=False),
                        summary=f"Lỗi thực thi trong agent: {e}",
                    )

            future = self._executor.submit(_target_exec)
            try:
                # Chờ với timeout
                result = future.result(timeout=timeout_seconds)
            except TimeoutError:
                logger.warning(f"Task '{task_id}' trên agent '{self.agent_id}' đã vượt quá timeout {timeout_seconds}s.")
                self._cancel_event.set()
                result = failed_result(
                    request,
                    make_error(
                        ErrorCode.TIMEOUT,
                        f"Agent thực thi vượt quá thời gian tối đa {timeout_seconds} giây.",
                        retryable=True,
                    ),
                    summary="Task timeout.",
                )
            except Exception as e:
                logger.exception(f"Lỗi không xác định khi thực thi task '{task_id}': {e}")
                result = failed_result(
                    request,
                    make_error(ErrorCode.UNKNOWN_ERROR, str(e), retryable=False),
                    summary="Unknown execution error.",
                )

            if self._cancel_event.is_set() and result.status != TaskStatus.FAILED:
                result = failed_result(
                    request,
                    make_error(ErrorCode.UNKNOWN_ERROR, "Tác vụ bị hủy bởi yêu cầu từ Control Node.", retryable=False),
                    summary="Task cancelled.",
                )

            # 3. SAU KHI CHẠY: Upload các artifact tạo ra lên Artifact Store PC1
            if result.status == TaskStatus.COMPLETED:
                result = self._upload_agent_artifacts(request, result)

        # 4. GHI VÀO OUTBOX SQLITE TRƯỚC TIÊN
        self.outbox.save_result(
            task_id=task_id,
            callback_url=request.callback_url,
            secret=self.secret,
            result=result,
        )

        # 5. Giải phóng trạng thái Agent về IDLE ngay khi chạy xong
        with self._lock:
            self.current_task_id = None
            self.status = AgentStatus.IDLE
            self._cancel_event.clear()

        # 6. Kích hoạt flush outbox trong thread riêng không block agent
        def _bg_flush():
            try:
                asyncio.run(self.outbox.flush_task(task_id))
            except Exception as e:
                logger.debug(f"Flush outbox task {task_id} gặp lỗi (sẽ gửi lại qua background drainer): {e}")

        threading.Thread(target=_bg_flush, daemon=True).start()

        logger.info(f"Agent '{self.agent_id}' đã hoàn tất task '{task_id}' (status={result.status.value}).")


class RunnerRegistry:
    """Quản lý danh sách các AgentRunner trên Worker Host."""

    def __init__(
        self,
        outbox: OutboxService,
        secret: str,
        control_url: str = "http://127.0.0.1:8000",
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self.outbox = outbox
        self.secret = secret
        self.control_url = control_url
        self.http_client = http_client
        self._runners: Dict[str, AgentRunner] = {}
        self._lock = threading.RLock()

    def register_runner(
        self,
        agent_id: str,
        role: AgentRole,
        agent_instance: Any,
        control_url: Optional[str] = None,
    ) -> None:
        """Đăng ký một AgentRunner."""
        with self._lock:
            runner = AgentRunner(
                agent_id=agent_id,
                role=role,
                agent_instance=agent_instance,
                outbox=self.outbox,
                secret=self.secret,
                control_url=control_url or self.control_url,
                http_client=self.http_client,
            )
            self._runners[agent_id] = runner

    def get_runner(self, agent_id: str) -> Optional[AgentRunner]:
        with self._lock:
            return self._runners.get(agent_id)

    def is_agent_busy(self, agent_id: str) -> bool:
        runner = self.get_runner(agent_id)
        return runner.is_busy() if runner else False

    def submit_task(self, request: DispatchRequest) -> bool:
        runner = self.get_runner(request.agent_id)
        if not runner:
            return False
        return runner.submit(request)

    def cancel_task(self, task_id: str, agent_id: Optional[str] = None) -> bool:
        with self._lock:
            if agent_id:
                runner = self.get_runner(agent_id)
                return runner.cancel(task_id) if runner else False
            for r in self._runners.values():
                if r.current_task_id == task_id:
                    return r.cancel(task_id)
            return False

    def get_agent_statuses(self) -> List[HeartbeatAgentStatus]:
        with self._lock:
            statuses = []
            for aid, r in self._runners.items():
                statuses.append(
                    HeartbeatAgentStatus(
                        agent_id=aid,
                        status=r.status,
                        current_task=r.current_task_id,
                    )
                )
            return statuses

    def get_running_task_ids(self) -> List[str]:
        with self._lock:
            return [r.current_task_id for r in self._runners.values() if r.current_task_id is not None]

    def get_running_tasks(self) -> List[str]:
        return self.get_running_task_ids()
