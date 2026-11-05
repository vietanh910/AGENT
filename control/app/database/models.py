"""control/app/database/models.py - Khai báo SQLAlchemy ORM Models cho Control Node PC1.

13 bảng dữ liệu:
- workers, agents, workflow_runs, tasks, task_dependencies, task_attempts,
  artifacts, approvals, audit_logs, agent_events, linear_links, projects, project_releases.

Tất cả bảng dùng InnoDB, charset utf8mb4, collation utf8mb4_unicode_ci.
Thời gian UTC dạng DATETIME(6), cột linh hoạt dùng JSON của MySQL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import DATETIME, JSON
from sqlalchemy.orm import relationship

from control.app.database.session import Base

MYSQL_TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def utc_now() -> datetime:
    """Trả về thời gian UTC có timezone."""
    return datetime.now(timezone.utc)


class WorkerModel(Base):
    """Bảng lưu trữ thông tin các Worker Host đã đăng ký với PC1."""
    __tablename__ = "workers"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    worker_id = Column(String(64), primary_key=True)
    machine_id = Column(String(32), nullable=False)
    base_url = Column(String(255), nullable=False)
    status = Column(String(32), nullable=False, default="ONLINE")
    contract_version = Column(String(32), nullable=False)
    worker_version = Column(String(32), nullable=False)
    last_heartbeat_at = Column(DATETIME(fsp=6), nullable=True)
    registered_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    updated_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now)


class AgentModel(Base):
    """Bảng lưu trữ trạng thái và năng lực của 10 Agent trong hệ thống."""
    __tablename__ = "agents"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    agent_id = Column(String(64), primary_key=True)
    role = Column(String(32), nullable=False)
    machine = Column(String(32), nullable=False)
    runner = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="IDLE")
    current_task = Column(String(64), nullable=True)
    capabilities = Column(JSON, nullable=False)
    updated_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now)


class WorkflowRunModel(Base):
    """Bảng lưu trữ các lần thực thi quy trình phân phối phần mềm (WorkflowRun)."""
    __tablename__ = "workflow_runs"
    __table_args__ = (
        Index("idx_runs_status", "status"),
        Index("idx_runs_project", "project_id"),
        MYSQL_TABLE_ARGS,
    )

    run_id = Column(String(64), primary_key=True)
    project_id = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="CREATED")
    brief = Column(Text, nullable=False)
    plan = Column(JSON, nullable=True)
    current_stage = Column(String(64), nullable=True)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    updated_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now)
    completed_at = Column(DATETIME(fsp=6), nullable=True)
    linear_project_id = Column(String(64), nullable=True)
    run_metadata = Column("metadata_json", JSON, nullable=True)

    tasks = relationship("TaskModel", back_populates="run", cascade="all, delete-orphan")
    approvals = relationship("ApprovalModel", back_populates="run", cascade="all, delete-orphan")


class ProjectModel(Base):
    """Stable project identity and the currently accepted release."""
    __tablename__ = "projects"
    __table_args__ = (MYSQL_TABLE_ARGS,)

    project_id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    root_run_id = Column(String(64), nullable=False)
    current_release_id = Column(String(64), nullable=True)
    active_change_run_id = Column(String(64), nullable=True)
    release_counter = Column(Integer, nullable=False, default=0)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    updated_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now)


class ProjectReleaseModel(Base):
    """Immutable artifact manifest; the original run remains untouched."""
    __tablename__ = "project_releases"
    __table_args__ = (
        UniqueConstraint("project_id", "version", name="uq_project_release_version"),
        UniqueConstraint("run_id", name="uq_project_release_run"),
        MYSQL_TABLE_ARGS,
    )

    release_id = Column(String(64), primary_key=True)
    project_id = Column(String(64), ForeignKey("projects.project_id"), nullable=False)
    run_id = Column(String(64), ForeignKey("workflow_runs.run_id"), nullable=False)
    version = Column(Integer, nullable=False)
    base_release_id = Column(String(64), nullable=True)
    manifest = Column(JSON, nullable=False)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)


class TaskModel(Base):
    """Bảng tasks lưu trữ trạng thái chi tiết của từng tác vụ được điều phối."""
    __tablename__ = "tasks"
    __table_args__ = (
        Index("idx_tasks_run_id", "run_id"),
        Index("idx_tasks_agent_id", "agent_id"),
        Index("idx_tasks_dispatch_state", "dispatch_state"),
        Index("idx_tasks_created_at", "created_at"),
        MYSQL_TABLE_ARGS,
    )

    task_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False)
    agent_id = Column(String(64), nullable=False)
    role = Column(String(32), nullable=False)
    action = Column(String(64), nullable=False)
    instruction = Column(Text, nullable=False)
    context = Column(JSON, nullable=True)
    dispatch_state = Column(String(32), nullable=False, default="QUEUED")
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=3)
    timeout_seconds = Column(Integer, nullable=False, default=300)
    deadline = Column(DATETIME(fsp=6), nullable=True)
    result = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)
    linear_issue_id = Column(String(64), nullable=True)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    updated_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now, onupdate=utc_now)

    run = relationship("WorkflowRunModel", back_populates="tasks")
    attempts_history = relationship("TaskAttemptModel", back_populates="task", cascade="all, delete-orphan")


class TaskDependencyModel(Base):
    """Bảng quan hệ phụ thuộc giữa các task trong đồ thị DAG."""
    __tablename__ = "task_dependencies"
    __table_args__ = (
        Index("idx_dep_depends_on", "depends_on_task_id"),
        MYSQL_TABLE_ARGS,
    )

    task_id = Column(String(64), ForeignKey("tasks.task_id", ondelete="CASCADE"), primary_key=True)
    depends_on_task_id = Column(String(64), ForeignKey("tasks.task_id", ondelete="CASCADE"), primary_key=True)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)


class TaskAttemptModel(Base):
    """Bảng ghi nhận lịch sử các lần dispatch của một task."""
    __tablename__ = "task_attempts"
    __table_args__ = (
        Index("idx_attempt_task", "task_id"),
        MYSQL_TABLE_ARGS,
    )

    attempt_id = Column(String(64), primary_key=True)
    task_id = Column(String(64), ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False)
    attempt_number = Column(Integer, nullable=False)
    dispatched_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    finished_at = Column(DATETIME(fsp=6), nullable=True)
    worker_id = Column(String(64), nullable=True)
    status = Column(String(32), nullable=False)
    error = Column(JSON, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    task = relationship("TaskModel", back_populates="attempts_history")


class ArtifactModel(Base):
    """Bảng lưu trữ thông tin các Artifact được tạo ra trong quá trình chạy."""
    __tablename__ = "artifacts"
    __table_args__ = (
        Index("idx_art_run_id", "run_id"),
        Index("idx_art_task_id", "task_id"),
        Index("idx_art_type", "type"),
        MYSQL_TABLE_ARGS,
    )

    artifact_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("workflow_runs.run_id", ondelete="SET NULL"), nullable=True)
    task_id = Column(String(64), ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True)
    name = Column(String(255), nullable=False)
    type = Column(String(64), nullable=False)
    version = Column(String(32), nullable=False, default="1.0")
    uri = Column(String(512), nullable=False)
    checksum = Column(String(64), nullable=True)
    created_by = Column(String(64), nullable=False)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    metadata_json = Column(JSON, nullable=True)


class ApprovalModel(Base):
    """Bảng lưu trữ các yêu cầu và quyết định phê duyệt tại cổng duyệt (gate)."""
    __tablename__ = "approvals"
    __table_args__ = (
        Index("idx_appr_run_id", "run_id"),
        Index("idx_appr_status", "status"),
        MYSQL_TABLE_ARGS,
    )

    approval_id = Column(String(64), primary_key=True)
    run_id = Column(String(64), ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False)
    gate = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="PENDING")
    decided_by = Column(String(128), nullable=True)
    decided_at = Column(DATETIME(fsp=6), nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)

    run = relationship("WorkflowRunModel", back_populates="approvals")


class AuditLogModel(Base):
    """Bảng ghi nhận nhật ký kiểm toán (Audit Log) cho các hành động nhạy cảm."""
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("idx_audit_run", "run_id"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_timestamp", "timestamp"),
        MYSQL_TABLE_ARGS,
    )

    id = Column(Integer().with_variant(BigInteger, "mysql"), primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=True)
    task_id = Column(String(64), nullable=True)
    agent_id = Column(String(64), nullable=True)
    actor = Column(String(128), nullable=True)
    action = Column(String(64), nullable=False)
    target = Column(String(255), nullable=True)
    result = Column(String(64), nullable=False)
    details = Column(JSON, nullable=True)
    timestamp = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
    prev_hash = Column(String(64), nullable=True)
    entry_hash = Column(String(64), nullable=True)


class AgentEventModel(Base):
    """Bảng ghi nhận các sự kiện hệ thống phát ra từ Agent và Worker."""
    __tablename__ = "agent_events"
    __table_args__ = (
        Index("idx_event_type", "event_type"),
        Index("idx_event_run", "run_id"),
        Index("idx_event_created", "created_at"),
        MYSQL_TABLE_ARGS,
    )

    id = Column(Integer().with_variant(BigInteger, "mysql"), primary_key=True, autoincrement=True)
    event_id = Column(String(64), nullable=False, unique=True)
    event_type = Column(String(64), nullable=False)
    run_id = Column(String(64), nullable=True)
    task_id = Column(String(64), nullable=True)
    worker_id = Column(String(64), nullable=True)
    payload = Column(JSON, nullable=False)
    created_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)


class LinearLinkModel(Base):
    """Bảng lưu trữ ánh xạ thực thể nội bộ với Issue/Project/Milestone trên Linear."""
    __tablename__ = "linear_links"
    __table_args__ = (
        Index("idx_linear_entity", "entity_type", "local_id"),
        Index("idx_linear_id", "linear_id"),
        MYSQL_TABLE_ARGS,
    )

    id = Column(Integer().with_variant(BigInteger, "mysql"), primary_key=True, autoincrement=True)
    entity_type = Column(String(32), nullable=False)
    local_id = Column(String(64), nullable=False)
    linear_id = Column(String(64), nullable=False)
    content_hash = Column(String(64), nullable=True)
    last_synced_at = Column(DATETIME(fsp=6), nullable=False, default=utc_now)
