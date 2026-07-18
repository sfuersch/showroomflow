"""Add system-wide image comparison mode switch."""

from alembic import op
import sqlalchemy as sa


revision = "0009_comparison_mode"
down_revision = "0008_carryover_credit_topups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_image_settings",
        sa.Column(
            "comparison_mode_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("system_image_settings", "comparison_mode_enabled")
