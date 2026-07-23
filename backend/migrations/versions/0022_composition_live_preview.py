"""Add background transforms and reusable preview cutouts."""

from alembic import op
import sqlalchemy as sa


revision = "0022_composition_preview"
down_revision = "0021_background_composition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, default in (
        ("background_zoom_percent", "100"),
        ("background_offset_x_percent", "0"),
        ("background_offset_y_percent", "0"),
    ):
        op.add_column(
            "backgrounds",
            sa.Column(name, sa.Integer(), nullable=False, server_default=default),
        )
        op.add_column(
            "background_orientation_compositions",
            sa.Column(name, sa.Integer(), nullable=True),
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
    for name in (
        "background_offset_y_percent",
        "background_offset_x_percent",
        "background_zoom_percent",
    ):
        op.drop_column("background_orientation_compositions", name)
        op.drop_column("backgrounds", name)
