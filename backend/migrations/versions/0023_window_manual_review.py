"""Add manual window correction and background positioning."""

from alembic import op
import sqlalchemy as sa


revision = "0023_window_manual_review"
down_revision = "0022_window_background_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backgrounds",
        sa.Column(
            "window_background_shift_percent",
            sa.Integer(),
            nullable=False,
            server_default="14",
        ),
    )
    op.add_column(
        "background_orientation_compositions",
        sa.Column("window_background_shift_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "photo_assets",
        sa.Column("window_mask_object_key", sa.String(length=500), nullable=True),
    )
    op.add_column(
        "photo_assets",
        sa.Column(
            "window_mask_is_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "photo_assets",
        sa.Column("window_background_shift_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "photo_assets",
        sa.Column(
            "quality_review_required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "photo_assets",
        sa.Column("quality_review_reason", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("photo_assets", "quality_review_reason")
    op.drop_column("photo_assets", "quality_review_required")
    op.drop_column("photo_assets", "window_background_shift_percent")
    op.drop_column("photo_assets", "window_mask_is_manual")
    op.drop_column("photo_assets", "window_mask_object_key")
    op.drop_column(
        "background_orientation_compositions", "window_background_shift_percent"
    )
    op.drop_column("backgrounds", "window_background_shift_percent")
