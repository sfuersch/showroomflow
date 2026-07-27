"""Add a per-photo shadow override for quality corrections.

Revision ID: 0032_photo_shadow_override
Revises: 0031_optimized_correction
"""

from alembic import op
import sqlalchemy as sa


revision = "0032_photo_shadow_override"
down_revision = "0031_optimized_correction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "photo_assets",
        sa.Column(
            "vehicle_shadow_opacity_percent",
            sa.Integer(),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("photo_assets", "vehicle_shadow_opacity_percent")
