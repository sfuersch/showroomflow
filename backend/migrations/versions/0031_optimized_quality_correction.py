"""Add manual correction settings for optimized vehicle photos.

Revision ID: 0031_optimized_correction
Revises: 0030_composition_preview
"""

from alembic import op
import sqlalchemy as sa


revision = "0031_optimized_correction"
down_revision = "0030_composition_preview"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "photo_assets",
        sa.Column(
            "vehicle_mask_is_manual",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "photo_assets",
        sa.Column(
            "vehicle_scale_percent",
            sa.Integer(),
            nullable=False,
            server_default="100",
        ),
    )
    op.add_column(
        "photo_assets",
        sa.Column(
            "vehicle_offset_x_percent",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "photo_assets",
        sa.Column(
            "vehicle_offset_y_percent",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("photo_assets", "vehicle_offset_y_percent")
    op.drop_column("photo_assets", "vehicle_offset_x_percent")
    op.drop_column("photo_assets", "vehicle_scale_percent")
    op.drop_column("photo_assets", "vehicle_mask_is_manual")
