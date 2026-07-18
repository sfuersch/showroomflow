"""Add archived ZIP export state."""

from alembic import op
import sqlalchemy as sa


revision = "0011_zip_exports"
down_revision = "0010_overlay_supplemental_assets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "export_runs",
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
    )
    op.add_column(
        "export_runs",
        sa.Column("object_key", sa.String(length=500), nullable=True),
    )
    op.add_column("export_runs", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.add_column(
        "export_runs",
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_export_runs_object_key", "export_runs", ["object_key"])


def downgrade() -> None:
    op.drop_constraint("uq_export_runs_object_key", "export_runs", type_="unique")
    op.drop_column("export_runs", "completed_at")
    op.drop_column("export_runs", "size_bytes")
    op.drop_column("export_runs", "object_key")
    op.drop_column("export_runs", "status")
