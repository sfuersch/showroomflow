"""Add original photo assets and upload progress."""

from alembic import op
import sqlalchemy as sa

revision = "0004_photo_assets"
down_revision = "0003_capture_configuration"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "photo_assets",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("vehicle_job_id", sa.Uuid(), sa.ForeignKey("vehicle_jobs.id"), nullable=False),
        sa.Column("capture_step_id", sa.Uuid(), sa.ForeignKey("capture_steps.id"), nullable=False),
        sa.Column("captured_by_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("original_object_key", sa.String(500), nullable=False, unique=True),
        sa.Column("original_content_type", sa.String(100), nullable=False),
        sa.Column("expected_size_bytes", sa.Integer(), nullable=False),
        sa.Column("original_size_bytes", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_selected", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "vehicle_job_id",
            "capture_step_id",
            "revision",
            name="uq_photo_asset_revision",
        ),
    )
    op.create_index("ix_photo_assets_vehicle_job_id", "photo_assets", ["vehicle_job_id"])
    op.create_index("ix_photo_assets_capture_step_id", "photo_assets", ["capture_step_id"])
    op.create_index("ix_photo_assets_captured_by_id", "photo_assets", ["captured_by_id"])


def downgrade() -> None:
    op.drop_table("photo_assets")
