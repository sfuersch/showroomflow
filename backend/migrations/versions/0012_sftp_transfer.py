"""Add per-dealership SFTP settings and export transfer state."""

from alembic import op
import sqlalchemy as sa


revision = "0012_sftp_transfer"
down_revision = "0011_zip_exports"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dealership_sftp_settings",
        sa.Column("dealership_id", sa.Uuid(), nullable=False),
        sa.Column("host", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("port", sa.Integer(), nullable=False, server_default="22"),
        sa.Column("username", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.Column("remote_directory", sa.String(length=500), nullable=False, server_default="/"),
        sa.Column("host_key_fingerprint", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_successful", sa.Boolean(), nullable=True),
        sa.Column("last_test_error", sa.String(length=1000), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dealership_id"], ["dealerships.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("dealership_id"),
    )
    op.add_column(
        "export_runs",
        sa.Column(
            "transfer_status", sa.String(length=32), nullable=False, server_default="not_requested"
        ),
    )
    op.add_column(
        "export_runs",
        sa.Column("transfer_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("export_runs", sa.Column("transferred_at", sa.DateTime(timezone=True)))
    op.add_column("export_runs", sa.Column("remote_path", sa.String(length=700)))
    op.add_column("export_runs", sa.Column("transfer_error", sa.String(length=1000)))


def downgrade() -> None:
    op.drop_column("export_runs", "transfer_error")
    op.drop_column("export_runs", "remote_path")
    op.drop_column("export_runs", "transferred_at")
    op.drop_column("export_runs", "transfer_attempts")
    op.drop_column("export_runs", "transfer_status")
    op.drop_table("dealership_sftp_settings")
