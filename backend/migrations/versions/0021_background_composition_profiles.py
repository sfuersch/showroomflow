"""Add per-background composition defaults and orientation overrides."""

from alembic import op
import sqlalchemy as sa


revision = "0021_background_composition_profiles"
down_revision = "0020_scene_capture_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backgrounds",
        sa.Column(
            "contour_target_area_percent",
            sa.Integer(),
            nullable=False,
            server_default="36",
        ),
    )
    op.add_column(
        "backgrounds",
        sa.Column(
            "contour_max_width_percent",
            sa.Integer(),
            nullable=False,
            server_default="78",
        ),
    )
    op.add_column(
        "backgrounds",
        sa.Column(
            "contour_max_height_percent",
            sa.Integer(),
            nullable=False,
            server_default="72",
        ),
    )

    # Preserve the currently configured global contour values for every existing
    # background. New backgrounds use the established defaults above.
    connection = op.get_bind()
    settings = connection.execute(
        sa.text(
            "SELECT contour_target_area_percent, contour_max_width_percent, "
            "contour_max_height_percent FROM system_image_settings WHERE id = 1"
        )
    ).mappings().first()
    if settings:
        connection.execute(
            sa.text(
                "UPDATE backgrounds SET contour_target_area_percent=:target, "
                "contour_max_width_percent=:width, contour_max_height_percent=:height"
            ),
            {
                "target": settings["contour_target_area_percent"],
                "width": settings["contour_max_width_percent"],
                "height": settings["contour_max_height_percent"],
            },
        )

    op.create_table(
        "background_orientation_compositions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "background_id",
            sa.Uuid(),
            sa.ForeignKey("backgrounds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "orientation_id",
            sa.Uuid(),
            sa.ForeignKey("orientations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("contour_target_area_percent", sa.Integer(), nullable=True),
        sa.Column("contour_max_width_percent", sa.Integer(), nullable=True),
        sa.Column("contour_max_height_percent", sa.Integer(), nullable=True),
        sa.Column("vehicle_bottom_percent", sa.Integer(), nullable=True),
        sa.Column("shadow_opacity_percent", sa.Integer(), nullable=True),
        sa.Column("reflection_opacity_percent", sa.Integer(), nullable=True),
        sa.Column("brightness_percent", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "background_id",
            "orientation_id",
            name="uq_background_orientation_composition",
        ),
    )
    op.create_index(
        "ix_background_orientation_compositions_background_id",
        "background_orientation_compositions",
        ["background_id"],
    )
    op.create_index(
        "ix_background_orientation_compositions_orientation_id",
        "background_orientation_compositions",
        ["orientation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_background_orientation_compositions_orientation_id",
        table_name="background_orientation_compositions",
    )
    op.drop_index(
        "ix_background_orientation_compositions_background_id",
        table_name="background_orientation_compositions",
    )
    op.drop_table("background_orientation_compositions")
    op.drop_column("backgrounds", "contour_max_height_percent")
    op.drop_column("backgrounds", "contour_max_width_percent")
    op.drop_column("backgrounds", "contour_target_area_percent")
