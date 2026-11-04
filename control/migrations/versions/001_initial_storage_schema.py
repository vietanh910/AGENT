"""Initial storage schema for PC1 Control Node (MySQL 8.4 LTS).

Revision ID: 001_initial_storage_schema
Revises:
Create Date: 2026-11-04 12:00:00

Gồm 11 bảng:
- workers, agents, workflow_runs, tasks, task_dependencies, task_attempts,
  artifacts, approvals, audit_logs, agent_events, linear_links.

Hướng dẫn khôi phục sự cố (MySQL không có DDL Transactional):
- Nếu migration bị gián đoạn giữa chừng, migration được thiết kế chạy lại an toàn
  (kiểm tra bảng tồn tại trước khi tạo mới).
- Để rollback thủ công nếu cần thiết: chạy 'alembic downgrade base' hoặc
  drop các bảng theo thứ tự ngược lại (bảng có khóa ngoại trước).
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME, JSON

# revision identifiers, used by Alembic.
revision: str = "001_initial_storage_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MYSQL_TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


def table_exists(table_name: str) -> bool:
    """Kiểm tra bảng đã tồn tại hay chưa."""
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return insp.has_table(table_name)


def upgrade() -> None:
    # 1. Bảng workers
    if not table_exists("workers"):
        op.create_table(
            "workers",
            sa.Column("worker_id", sa.String(64), primary_key=True),
            sa.Column("machine_id", sa.String(32), nullable=False),
            sa.Column("base_url", sa.String(255), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="ONLINE"),
            sa.Column("contract_version", sa.String(32), nullable=False),
            sa.Column("worker_version", sa.String(32), nullable=False),
            sa.Column("last_heartbeat_at", DATETIME(fsp=6), nullable=True),
            sa.Column("registered_at", DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )

    # 2. Bảng agents
    if not table_exists("agents"):
        op.create_table(
            "agents",
            sa.Column("agent_id", sa.String(64), primary_key=True),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("machine", sa.String(32), nullable=False),
            sa.Column("runner", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="IDLE"),
            sa.Column("current_task", sa.String(64), nullable=True),
            sa.Column("capabilities", JSON, nullable=False),
            sa.Column("updated_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )

    # 3. Bảng workflow_runs
    if not table_exists("workflow_runs"):
        op.create_table(
            "workflow_runs",
            sa.Column("run_id", sa.String(64), primary_key=True),
            sa.Column("project_id", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="CREATED"),
            sa.Column("brief", sa.Text(), nullable=False),
            sa.Column("plan", JSON, nullable=True),
            sa.Column("current_stage", sa.String(64), nullable=True),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", DATETIME(fsp=6), nullable=False),
            sa.Column("completed_at", DATETIME(fsp=6), nullable=True),
            sa.Column("linear_project_id", sa.String(64), nullable=True),
            sa.Column("metadata_json", JSON, nullable=True),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_runs_status", "workflow_runs", ["status"])
        op.create_index("idx_runs_project", "workflow_runs", ["project_id"])

    # 4. Bảng tasks
    if not table_exists("tasks"):
        op.create_table(
            "tasks",
            sa.Column("task_id", sa.String(64), primary_key=True),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False),
            sa.Column("agent_id", sa.String(64), nullable=False),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("instruction", sa.Text(), nullable=False),
            sa.Column("context", JSON, nullable=True),
            sa.Column("dispatch_state", sa.String(32), nullable=False, server_default="QUEUED"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="300"),
            sa.Column("deadline", DATETIME(fsp=6), nullable=True),
            sa.Column("result", JSON, nullable=True),
            sa.Column("error", JSON, nullable=True),
            sa.Column("linear_issue_id", sa.String(64), nullable=True),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_tasks_run_id", "tasks", ["run_id"])
        op.create_index("idx_tasks_agent_id", "tasks", ["agent_id"])
        op.create_index("idx_tasks_dispatch_state", "tasks", ["dispatch_state"])
        op.create_index("idx_tasks_created_at", "tasks", ["created_at"])

    # 5. Bảng task_dependencies
    if not table_exists("task_dependencies"):
        op.create_table(
            "task_dependencies",
            sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.task_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("depends_on_task_id", sa.String(64), sa.ForeignKey("tasks.task_id", ondelete="CASCADE"), primary_key=True),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_dep_depends_on", "task_dependencies", ["depends_on_task_id"])

    # 6. Bảng task_attempts
    if not table_exists("task_attempts"):
        op.create_table(
            "task_attempts",
            sa.Column("attempt_id", sa.String(64), primary_key=True),
            sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.task_id", ondelete="CASCADE"), nullable=False),
            sa.Column("attempt_number", sa.Integer(), nullable=False),
            sa.Column("dispatched_at", DATETIME(fsp=6), nullable=False),
            sa.Column("finished_at", DATETIME(fsp=6), nullable=True),
            sa.Column("worker_id", sa.String(64), nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("error", JSON, nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_attempt_task", "task_attempts", ["task_id"])

    # 7. Bảng artifacts
    if not table_exists("artifacts"):
        op.create_table(
            "artifacts",
            sa.Column("artifact_id", sa.String(64), primary_key=True),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.run_id", ondelete="SET NULL"), nullable=True),
            sa.Column("task_id", sa.String(64), sa.ForeignKey("tasks.task_id", ondelete="SET NULL"), nullable=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("type", sa.String(64), nullable=False),
            sa.Column("version", sa.String(32), nullable=False, server_default="1.0"),
            sa.Column("uri", sa.String(512), nullable=False),
            sa.Column("checksum", sa.String(64), nullable=True),
            sa.Column("created_by", sa.String(64), nullable=False),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            sa.Column("metadata_json", JSON, nullable=True),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_art_run_id", "artifacts", ["run_id"])
        op.create_index("idx_art_task_id", "artifacts", ["task_id"])
        op.create_index("idx_art_type", "artifacts", ["type"])

    # 8. Bảng approvals
    if not table_exists("approvals"):
        op.create_table(
            "approvals",
            sa.Column("approval_id", sa.String(64), primary_key=True),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.run_id", ondelete="CASCADE"), nullable=False),
            sa.Column("gate", sa.String(64), nullable=False),
            sa.Column("status", sa.String(32), nullable=False, server_default="PENDING"),
            sa.Column("decided_by", sa.String(128), nullable=True),
            sa.Column("decided_at", DATETIME(fsp=6), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_appr_run_id", "approvals", ["run_id"])
        op.create_index("idx_appr_status", "approvals", ["status"])

    # 9. Bảng audit_logs
    if not table_exists("audit_logs"):
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(64), nullable=True),
            sa.Column("task_id", sa.String(64), nullable=True),
            sa.Column("agent_id", sa.String(64), nullable=True),
            sa.Column("actor", sa.String(128), nullable=True),
            sa.Column("action", sa.String(64), nullable=False),
            sa.Column("target", sa.String(255), nullable=True),
            sa.Column("result", sa.String(64), nullable=False),
            sa.Column("details", JSON, nullable=True),
            sa.Column("timestamp", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_audit_run", "audit_logs", ["run_id"])
        op.create_index("idx_audit_action", "audit_logs", ["action"])
        op.create_index("idx_audit_timestamp", "audit_logs", ["timestamp"])

    # 10. Bảng agent_events
    if not table_exists("agent_events"):
        op.create_table(
            "agent_events",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("event_id", sa.String(64), nullable=False, unique=True),
            sa.Column("event_type", sa.String(64), nullable=False),
            sa.Column("run_id", sa.String(64), nullable=True),
            sa.Column("task_id", sa.String(64), nullable=True),
            sa.Column("worker_id", sa.String(64), nullable=True),
            sa.Column("payload", JSON, nullable=False),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_event_type", "agent_events", ["event_type"])
        op.create_index("idx_event_run", "agent_events", ["run_id"])
        op.create_index("idx_event_created", "agent_events", ["created_at"])

    # 11. Bảng linear_links
    if not table_exists("linear_links"):
        op.create_table(
            "linear_links",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("entity_type", sa.String(32), nullable=False),
            sa.Column("local_id", sa.String(64), nullable=False),
            sa.Column("linear_id", sa.String(64), nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("last_synced_at", DATETIME(fsp=6), nullable=False),
            **MYSQL_TABLE_ARGS,
        )
        op.create_index("idx_linear_entity", "linear_links", ["entity_type", "local_id"])
        op.create_index("idx_linear_id", "linear_links", ["linear_id"])


def downgrade() -> None:
    # Xóa theo thứ tự phụ thuộc (bảng con trước, bảng cha sau)
    tables_to_drop = [
        "linear_links",
        "agent_events",
        "audit_logs",
        "approvals",
        "artifacts",
        "task_attempts",
        "task_dependencies",
        "tasks",
        "workflow_runs",
        "agents",
        "workers",
    ]
    for tbl in tables_to_drop:
        if table_exists(tbl):
            op.drop_table(tbl)
