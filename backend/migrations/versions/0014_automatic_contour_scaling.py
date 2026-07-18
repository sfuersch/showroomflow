"""Replace perspective profiles with automatic contour scaling."""

from alembic import op
import sqlalchemy as sa


revision = "0014_automatic_contour_scaling"
down_revision = "0013_central_scaling_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_image_settings",
        sa.Column(
            "contour_target_area_percent", sa.Integer(), nullable=False, server_default="36"
        ),
    )
    op.add_column(
        "system_image_settings",
        sa.Column(
            "contour_max_width_percent", sa.Integer(), nullable=False, server_default="78"
        ),
    )
    op.add_column(
        "system_image_settings",
        sa.Column(
            "contour_max_height_percent", sa.Integer(), nullable=False, server_default="72"
        ),
    )
    op.drop_column("system_image_settings", "vehicle_scale_default_percent")
    op.drop_column("system_image_settings", "vehicle_scale_rear_percent")
    op.drop_column("system_image_settings", "vehicle_scale_side_percent")
    op.drop_column("system_image_settings", "vehicle_scale_diagonal_percent")
    op.drop_column("system_image_settings", "vehicle_scale_front_percent")


def downgrade() -> None:
    op.add_column(
        "system_image_settings",
        sa.Column("vehicle_scale_front_percent", sa.Integer(), nullable=False, server_default="52"),
    )
    op.add_column(
        "system_image_settings",
        sa.Column(
            "vehicle_scale_diagonal_percent", sa.Integer(), nullable=False, server_default="64"
        ),
    )
    op.add_column(
        "system_image_settings",
        sa.Column("vehicle_scale_side_percent", sa.Integer(), nullable=False, server_default="72"),
    )
    op.add_column(
        "system_image_settings",
        sa.Column("vehicle_scale_rear_percent", sa.Integer(), nullable=False, server_default="54"),
    )
    op.add_column(
        "system_image_settings",
        sa.Column(
            "vehicle_scale_default_percent", sa.Integer(), nullable=False, server_default="64"
        ),
    )
    op.drop_column("system_image_settings", "contour_max_height_percent")
    op.drop_column("system_image_settings", "contour_max_width_percent")
    op.drop_column("system_image_settings", "contour_target_area_percent")
