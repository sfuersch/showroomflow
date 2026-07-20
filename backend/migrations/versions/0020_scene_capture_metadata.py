"""Add camera pose metadata and background scene calibration."""

from alembic import op
import sqlalchemy as sa


revision = "0020_scene_capture_metadata"
down_revision = "0019_orientation_templates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("photo_assets", sa.Column("capture_metadata", sa.JSON(), nullable=True))
    op.add_column(
        "backgrounds",
        sa.Column(
            "scene_projection_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "backgrounds",
        sa.Column("scene_horizon_percent", sa.Integer(), nullable=False, server_default="43"),
    )
    op.add_column(
        "backgrounds",
        sa.Column(
            "scene_reference_vertical_degrees",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "backgrounds",
        sa.Column(
            "scene_perspective_strength_percent",
            sa.Integer(),
            nullable=False,
            server_default="35",
        ),
    )


def downgrade() -> None:
    op.drop_column("backgrounds", "scene_perspective_strength_percent")
    op.drop_column("backgrounds", "scene_reference_vertical_degrees")
    op.drop_column("backgrounds", "scene_horizon_percent")
    op.drop_column("backgrounds", "scene_projection_enabled")
    op.drop_column("photo_assets", "capture_metadata")
