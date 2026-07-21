"""Track outbound image-provider API requests."""

from alembic import op
import sqlalchemy as sa


revision = "0026_external_api_usage"
down_revision = "0025_quality_review_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_api_usages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("operation", sa.String(length=64), nullable=False),
        sa.Column("dealership_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_job_id", sa.Uuid(), nullable=False),
        sa.Column("photo_asset_id", sa.Uuid(), nullable=True),
        sa.Column("processing_attempt", sa.Integer(), nullable=True),
        sa.Column("sandbox", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.String(length=500), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dealership_id"], ["dealerships.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_job_id"], ["vehicle_jobs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["photo_asset_id"], ["photo_assets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "provider",
        "operation",
        "dealership_id",
        "vehicle_job_id",
        "photo_asset_id",
        "sandbox",
        "outcome",
        "occurred_at",
    ):
        op.create_index(
            f"ix_external_api_usages_{column}",
            "external_api_usages",
            [column],
            unique=False,
        )


def downgrade() -> None:
    op.drop_table("external_api_usages")
