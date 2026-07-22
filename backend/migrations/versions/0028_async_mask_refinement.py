"""Defer manual mask edge refinement to the image worker."""

from alembic import op
import sqlalchemy as sa


revision = "0028_async_mask_refinement"
down_revision = "0027_web_push_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "photo_assets",
        sa.Column(
            "window_mask_refine_edges",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("photo_assets", "window_mask_refine_edges")
