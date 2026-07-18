"""Require an explicit capture completion before automatic export."""

from alembic import op
import sqlalchemy as sa


revision = "0016_capture_completion"
down_revision = "0015_photo_thumbnails"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "vehicle_jobs",
        sa.Column("capture_completed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("vehicle_jobs", "capture_completed_at")
