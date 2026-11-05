"""Stable projects and immutable releases for subsequent change requests."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME, JSON

revision = "002_project_releases"
down_revision = "001_initial_storage_schema"
branch_labels = None
depends_on = None


def upgrade():
    options = dict(mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_unicode_ci")
    existing = sa.inspect(op.get_bind()).get_table_names()
    if "projects" not in existing:
        op.create_table("projects",
            sa.Column("project_id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("root_run_id", sa.String(64), nullable=False),
            sa.Column("current_release_id", sa.String(64)),
            sa.Column("active_change_run_id", sa.String(64)),
            sa.Column("release_counter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            sa.Column("updated_at", DATETIME(fsp=6), nullable=False), **options)
    if "project_releases" not in existing:
        op.create_table("project_releases",
            sa.Column("release_id", sa.String(64), primary_key=True),
            sa.Column("project_id", sa.String(64), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("run_id", sa.String(64), sa.ForeignKey("workflow_runs.run_id"), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("base_release_id", sa.String(64)),
            sa.Column("manifest", JSON, nullable=False),
            sa.Column("created_at", DATETIME(fsp=6), nullable=False),
            sa.UniqueConstraint("project_id", "version", name="uq_project_release_version"),
            sa.UniqueConstraint("run_id", name="uq_project_release_run"), **options)


def downgrade():
    op.drop_table("project_releases")
    op.drop_table("projects")
