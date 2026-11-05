"""Dịch vụ quản lý kho lưu trữ Artifact tập trung trên PC1 (Artifact Store).

Chức năng:
1. Lưu trữ và xác thực tệp artifact tải lên (multipart: file + metadata):
   - Làm sạch tên file, chống path traversal, chống ghi đè bản cũ.
   - Kiểm tra giới hạn kích thước file từ MAX_UPLOAD_MB.
   - Tính toán và xác thực mã băm SHA-256 (từ chối 422 nếu checksum không khớp).
   - Lưu trữ vật lý: control/data/artifacts/<run_id>/<artifact_id>-<clean_filename>.
   - Lưu siêu dữ liệu vào bảng artifacts (MySQL) với URI dạng artifact://<artifact_id>.
2. Tải tệp artifact với header X-Checksum-SHA256.
3. Liệt kê artifact theo run, hỗ trợ lọc type và lấy bản mới nhất (latest=true).
4. Hàm select_input_artifacts(session, task): tự động chọn các artifact đầu vào mới nhất
   trong run dựa trên loại đầu vào của action trong ACTION_MAP / luồng chuyển giao.
"""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from agent_contract.enums import AgentRole, ArtifactType, ErrorCode
from agent_contract.models import ArtifactRef
from control.app.config import settings
from control.app.database.models import ArtifactModel, TaskModel
from control.app.database.repositories import ArtifactRepository

# Thư mục gốc lưu trữ Artifact trên PC1
DEFAULT_ARTIFACT_STORE_DIR = Path("control/data/artifacts")

# Ánh xạ loại Artifact đầu vào cần thiết cho từng Action trong quy trình
ACTION_INPUT_TYPES_MAP: dict[str, list[ArtifactType]] = {
    # PO
    "define_backlog": [],
    "accept_release": [ArtifactType.TEST_REPORT, ArtifactType.SECURITY_REPORT, ArtifactType.DEPLOYMENT],
    # BA
    "write_requirements": [ArtifactType.BACKLOG],
    "write_user_stories": [ArtifactType.REQUIREMENT],
    # SA
    "design_architecture": [ArtifactType.REQUIREMENT, ArtifactType.USER_STORY],
    # DBA
    "design_schema": [ArtifactType.ARCHITECTURE],
    # DEV
    "implement_project": [ArtifactType.ARCHITECTURE, ArtifactType.DATABASE_SCHEMA, ArtifactType.TEST_CASE],
    "implement_feature": [ArtifactType.USER_STORY, ArtifactType.ARCHITECTURE, ArtifactType.DATABASE_SCHEMA],
    "fix_bugs": [ArtifactType.TEST_REPORT, ArtifactType.SOURCE_CODE],
    # TESTER
    "create_test_cases": [ArtifactType.ARCHITECTURE, ArtifactType.DATABASE_SCHEMA],
    "run_test_suite": [ArtifactType.SOURCE_CODE, ArtifactType.TEST_CASE],
    # SECURITY
    "security_review": [ArtifactType.SOURCE_CODE, ArtifactType.ARCHITECTURE],
    # DEVOPS
    "generate_ci_config": [ArtifactType.SOURCE_CODE],
    "build_image": [ArtifactType.SOURCE_CODE, ArtifactType.DEPLOYMENT],
    "deploy": [ArtifactType.DEPLOYMENT],
    "health_check": [ArtifactType.DEPLOYMENT],
    # SCRUM_MASTER
    "track_blockers": [ArtifactType.DOCUMENTATION],
    "run_sprint_review": [ArtifactType.TEST_REPORT, ArtifactType.DOCUMENTATION],
    "run_retrospective": [ArtifactType.TEST_REPORT, ArtifactType.DOCUMENTATION],
    # PM
    "analyze_requirement": [],
    "create_plan": [],
    "decompose_tasks": [ArtifactType.PLAN],
    "report_status": [],
}


class ArtifactStoreError(Exception):
    """Ngoại lệ khi thao tác với Artifact Store."""

    def __init__(self, message: str, status_code: int = 400, code: ErrorCode = ErrorCode.ARTIFACT_ERROR) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def sanitize_filename(filename: str) -> str:
    """Làm sạch tên file để loại trừ path traversal, ký tự đặc biệt độc hại."""
    if not filename or not filename.strip():
        return "unnamed_artifact.dat"

    # Chuẩn hóa dấu phân cách
    name = filename.replace("\\", "/").strip()
    name = os.path.basename(name)

    # Loại bỏ các ký tự nguy hiểm, chỉ giữ chữ cái, số, dấu gạch ngang, gạch dưới, chấm
    cleaned = re.sub(r"[^\w\.\-]", "_", name)
    # Loại bỏ chuỗi .. nếu còn sót
    cleaned = re.sub(r"\.{2,}", ".", cleaned)
    cleaned = cleaned.strip("._")
    if not cleaned:
        cleaned = "artifact.dat"
    return cleaned


def compute_sha256(content: bytes) -> str:
    """Tính mã băm SHA-256 cho chuỗi bytes."""
    h = hashlib.sha256()
    h.update(content)
    return h.hexdigest()


class ArtifactStoreService:
    """Dịch vụ lưu trữ và truy vấn Artifact trên PC1."""

    def __init__(self, base_dir: Path | str | None = None) -> None:
        self.base_dir = Path(base_dir) if base_dir else DEFAULT_ARTIFACT_STORE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_artifact(
        self,
        session: Session,
        content: bytes,
        filename: str,
        run_id: str,
        task_id: str | None,
        artifact_type: str | ArtifactType,
        created_by: str,
        artifact_id: str | None = None,
        version: str = "1.0",
        expected_checksum: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactModel:
        """Lưu trữ vật lý tệp artifact và ghi bản ghi vào cơ sở dữ liệu."""
        # 1. Kiểm tra kích thước file
        max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
        if len(content) > max_bytes:
            raise ArtifactStoreError(
                f"Kích thước file ({len(content)} bytes) vượt quá giới hạn cho phép ({settings.MAX_UPLOAD_MB}MB)",
                status_code=413,
                code=ErrorCode.ARTIFACT_ERROR,
            )

        # 2. Tính mã băm SHA-256 và đối chiếu
        actual_checksum = compute_sha256(content)
        if expected_checksum and expected_checksum.strip().lower() != actual_checksum.lower():
            raise ArtifactStoreError(
                f"Checksum SHA-256 không khớp! Client gửi: {expected_checksum}, Thực tế: {actual_checksum}",
                status_code=422,
                code=ErrorCode.VALIDATION_ERROR,
            )

        # 3. Tạo ID và URI cho artifact
        art_id = artifact_id or f"ART-{uuid.uuid4().hex[:12].upper()}"
        clean_name = sanitize_filename(filename)
        existing = ArtifactRepository.get_artifact(session, art_id)
        type_value = artifact_type.value if isinstance(artifact_type, ArtifactType) else str(artifact_type)
        if existing and (existing.run_id, existing.task_id, existing.checksum, existing.type,
                         existing.name, existing.version, existing.created_by) == (
                run_id, task_id, actual_checksum, type_value, clean_name, version, created_by):
            return existing  # Retry of the same immutable upload, never an overwrite.

        # 4. Lưu vật lý tại control/data/artifacts/<run_id>/<art_id>-<clean_name>
        run_dir = (self.base_dir / (run_id or "global")).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)

        target_file = run_dir / f"{art_id}-{clean_name}"
        if target_file.exists() and (existing is not None or compute_sha256(target_file.read_bytes()) != actual_checksum):
            raise ArtifactStoreError(
                f"Artifact với ID '{art_id}' và tên '{clean_name}' đã tồn tại! Không được phép ghi đè.",
                status_code=409,
                code=ErrorCode.ARTIFACT_ERROR,
            )

        if not target_file.exists():
            with open(target_file, "wb") as f:
                f.write(content)

        # 5. Lưu siêu dữ liệu vào DB
        art_type_str = artifact_type.value if isinstance(artifact_type, ArtifactType) else str(artifact_type)
        uri = f"artifact://{art_id}"

        # Kiểm tra nếu DB đã có bản ghi này
        existing_model = ArtifactRepository.get_artifact(session, art_id)
        if existing_model:
            raise ArtifactStoreError(
                f"Artifact ID '{art_id}' đã tồn tại trong cơ sở dữ liệu!",
                status_code=409,
                code=ErrorCode.ARTIFACT_ERROR,
            )

        meta = dict(metadata or {})
        meta["filename"] = clean_name
        meta["file_path"] = str(target_file.relative_to(Path(".").resolve()) if target_file.is_relative_to(Path(".").resolve()) else target_file)
        meta["size_bytes"] = len(content)
        # A content-addressed second copy lets PM repair a lost handoff without changing its identity.
        recovery_path = (self.base_dir / "_recovery" / actual_checksum).resolve()
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        if not recovery_path.exists():
            recovery_path.write_bytes(content)
        meta["recovery_copy"] = str(recovery_path)

        model = ArtifactRepository.save_artifact(
            session=session,
            data={
                "artifact_id": art_id,
                "run_id": run_id,
                "task_id": task_id,
                "name": clean_name,
                "type": art_type_str,
                "version": version,
                "uri": uri,
                "checksum": actual_checksum,
                "created_by": created_by,
                "metadata": meta,
            },
        )
        return model

    def get_artifact_file_path(self, session: Session, artifact_id: str) -> tuple[Path, ArtifactModel]:
        """Lấy đường dẫn tệp vật lý và model của artifact."""
        model = ArtifactRepository.get_artifact(session, artifact_id)
        if not model:
            raise ArtifactStoreError(f"Không tìm thấy Artifact '{artifact_id}'", status_code=404)

        # Tìm file trên đĩa
        run_dir = (self.base_dir / (model.run_id or "global")).resolve()
        clean_name = sanitize_filename(model.name)
        expected_path = run_dir / f"{model.artifact_id}-{clean_name}"

        if not expected_path.exists():
            # Thử tìm file bắt đầu bằng artifact_id- trong run_dir
            candidates = list(run_dir.glob(f"{model.artifact_id}-*"))
            if candidates:
                expected_path = candidates[0]
            else:
                raise ArtifactStoreError(
                    f"Tệp vật lý cho Artifact '{artifact_id}' không tồn tại trên máy chủ",
                    status_code=404,
                )

        return expected_path, model

    def list_artifacts(
        self,
        session: Session,
        run_id: str,
        artifact_type: str | None = None,
        latest: bool = True,
    ) -> list[ArtifactModel]:
        """Liệt kê danh sách artifact theo run."""
        return ArtifactRepository.list_by_run(
            session=session,
            run_id=run_id,
            artifact_type=artifact_type,
            latest=latest,
        )


def model_to_artifact_ref(model: ArtifactModel) -> ArtifactRef:
    """Chuyển đổi ArtifactModel (SQLAlchemy) sang ArtifactRef (Pydantic)."""
    type_enum = ArtifactType(model.type) if model.type in [e.value for e in ArtifactType] else ArtifactType.OTHER
    return ArtifactRef(
        artifact_id=model.artifact_id,
        name=model.name,
        type=type_enum,
        version=model.version,
        uri=model.uri,
        created_by=model.created_by,
        created_at=model.created_at,
        checksum=model.checksum,
        metadata=model.metadata_json or {},
    )


def select_input_artifacts(session: Session, task: TaskModel) -> list[ArtifactRef]:
    """Chọn các artifact đầu vào cho một task dựa theo action và run_id hiện tại.

    Nguyên tắc:
    - Tra cứu danh sách loại artifact đầu vào cần thiết cho task.action.
    - Với mỗi loại, truy vấn bản artifact mới nhất (latest=True) trong run_id của task.
    - Trả về danh sách ArtifactRef hoàn chỉnh để đưa vào TaskRequest/DispatchRequest.
    """
    if not task.run_id:
        return []

    # 1. Xác định các loại artifact cần thiết
    required_types: list[ArtifactType] = []

    # Nếu trong task.context có chỉ định rõ ràng
    if task.context and isinstance(task.context, dict) and "input_artifact_types" in task.context:
        types_raw = task.context["input_artifact_types"]
        for t in types_raw:
            try:
                required_types.append(ArtifactType(t))
            except ValueError:
                pass
    else:
        # Tra cứu từ bảng ACTION_INPUT_TYPES_MAP
        required_types = ACTION_INPUT_TYPES_MAP.get(task.action, [])

    if not required_types:
        return []

    # Explicit bindings keep module documents and exact code revisions together.
    bindings = (task.context or {}).get("input_artifact_sources") or {}
    baseline = (task.context or {}).get("baseline_artifact_ids") or {}
    if bindings or baseline:
        from sqlalchemy import select
        refs = []
        if baseline:
            from control.app.database.models import WorkflowRunModel
            from control.app.services.project_releases import pinned_baseline_artifacts, ReleaseError
            try:
                run = session.get(WorkflowRunModel, task.run_id)
                refs.extend(model_to_artifact_ref(a) for a in pinned_baseline_artifacts(session, run, baseline))
            except ReleaseError as exc:
                raise ArtifactStoreError(str(exc), code=ErrorCode.MISSING_CONTEXT) from exc
        for kind, source_ids in bindings.items():
            for source_id in source_ids:
                source_task = session.get(TaskModel, source_id)
                if not source_task or source_task.run_id != task.run_id or source_task.dispatch_state != "COMPLETED":
                    raise ArtifactStoreError(f"Input producer is not completed: {source_id}", code=ErrorCode.MISSING_CONTEXT)
                models = session.scalars(select(ArtifactModel).where(
                    ArtifactModel.run_id == task.run_id, ArtifactModel.task_id == source_id,
                    ArtifactModel.type == kind,
                ).order_by(ArtifactModel.created_at.desc())).all()
                declared = (source_task.result or {}).get("artifacts")
                if isinstance(declared, list):
                    current_ids = {ref.get("artifact_id") for ref in declared if isinstance(ref, dict)}
                    models = [model for model in models if model.artifact_id in current_ids]
                if not models:
                    raise ArtifactStoreError(f"Missing {kind} from {source_id}", code=ErrorCode.MISSING_CONTEXT)
                # A producer may output several files of the same type. Preserve all of them.
                names = set()
                for model in models:
                    if model.name not in names:
                        refs.append(model_to_artifact_ref(model))
                        names.add(model.name)
        for required_type in required_types:
            if not any(ref.type == required_type for ref in refs):
                raise ArtifactStoreError(f"Missing bound input: {required_type.value}", code=ErrorCode.MISSING_CONTEXT)
        return list({ref.artifact_id: ref for ref in refs}.values())

    # 2. Truy vấn bản mới nhất cho từng loại artifact trong cùng run_id
    selected_refs: list[ArtifactRef] = []
    seen_ids: set[str] = set()

    for req_type in required_types:
        latest_model = ArtifactRepository.get_latest_by_type(
            session=session,
            run_id=task.run_id,
            artifact_type=req_type.value,
        )
        if latest_model and latest_model.artifact_id not in seen_ids:
            seen_ids.add(latest_model.artifact_id)
            selected_refs.append(model_to_artifact_ref(latest_model))

    return selected_refs
