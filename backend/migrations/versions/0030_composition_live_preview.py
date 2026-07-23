"""Add live composition preview settings and reusable cutouts.

Revision ID: 0030_composition_preview
Revises: 0029_orientation_mask_prompts
"""

from alembic import op
import sqlalchemy as sa


revision = "0030_composition_preview"
down_revision = "0029_orientation_mask_prompts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backgrounds",
        sa.Column("background_zoom_percent", sa.Integer(), nullable=False, server_default="100"),
    )
    op.add_column(
        "backgrounds",
        sa.Column("background_offset_x_percent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "backgrounds",
        sa.Column("background_offset_y_percent", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "background_orientation_compositions",
        sa.Column("background_zoom_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "background_orientation_compositions",
        sa.Column("background_offset_x_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "background_orientation_compositions",
        sa.Column("background_offset_y_percent", sa.Integer(), nullable=True),
    )
    op.add_column(
        "photo_assets",
        sa.Column("preview_cutout_object_key", sa.String(length=500), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_assets_preview_cutout_object_key",
        "photo_assets",
        ["preview_cutout_object_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_photo_assets_preview_cutout_object_key",
        "photo_assets",
        type_="unique",
    )
    op.drop_column("photo_assets", "preview_cutout_object_key")
    op.drop_column(
        "background_orientation_compositions", "background_offset_y_percent"
    )
    op.drop_column(
        "background_orientation_compositions", "background_offset_x_percent"
    )
    op.drop_column("background_orientation_compositions", "background_zoom_percent")
    op.drop_column("backgrounds", "background_offset_y_percent")
    op.drop_column("backgrounds", "background_offset_x_percent")
    op.drop_column("backgrounds", "background_zoom_percent")
