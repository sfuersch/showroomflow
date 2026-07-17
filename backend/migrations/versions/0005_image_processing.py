"""Add asynchronous image processing state and showroom composition settings."""

from alembic import op
import sqlalchemy as sa


revision = "0005_image_processing"
down_revision = "0004_photo_assets"
branch_labels = None
depends_on = None


processing_status = sa.Enum(
    "NOT_REQUIRED",
    "PENDING",
    "QUEUED",
    "PROCESSING",
    "COMPLETED",
    "FAILED",
    name="processingstatus",
)


def upgrade() -> None:
    processing_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "backgrounds",
        sa.Column("vehicle_scale_percent", sa.Integer(), nullable=False, server_default="78"),
    )
    op.add_column(
        "backgrounds",
        sa.Column("vehicle_bottom_percent", sa.Integer(), nullable=False, server_default="90"),
    )
    op.add_column(
        "backgrounds",
        sa.Column("shadow_opacity_percent", sa.Integer(), nullable=False, server_default="32"),
    )
    op.add_column(
        "backgrounds",
        sa.Column("reflection_opacity_percent", sa.Integer(), nullable=False, server_default="10"),
    )
    op.add_column(
        "backgrounds",
        sa.Column("brightness_percent", sa.Integer(), nullable=False, server_default="100"),
    )

    op.add_column(
        "photo_assets",
        sa.Column(
            "processing_status",
            processing_status,
            nullable=False,
            server_default="NOT_REQUIRED",
        ),
    )
    op.add_column("photo_assets", sa.Column("processed_object_key", sa.String(500), nullable=True))
    op.add_column(
        "photo_assets", sa.Column("processed_content_type", sa.String(100), nullable=True)
    )
    op.add_column("photo_assets", sa.Column("processed_size_bytes", sa.Integer(), nullable=True))
    op.add_column(
        "photo_assets",
        sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("photo_assets", sa.Column("processing_error", sa.String(1000), nullable=True))
    op.add_column(
        "photo_assets",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "photo_assets",
        sa.Column("processing_completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_assets_processed_object_key", "photo_assets", ["processed_object_key"]
    )


def downgrade() -> None:
    op.drop_constraint("uq_photo_assets_processed_object_key", "photo_assets", type_="unique")
    for column in (
        "processing_completed_at",
        "processing_started_at",
        "processing_error",
        "processing_attempts",
        "processed_size_bytes",
        "processed_content_type",
        "processed_object_key",
        "processing_status",
    ):
        op.drop_column("photo_assets", column)
    processing_status.drop(op.get_bind(), checkfirst=True)

    for column in (
        "brightness_percent",
        "reflection_opacity_percent",
        "shadow_opacity_percent",
        "vehicle_bottom_percent",
        "vehicle_scale_percent",
    ):
        op.drop_column("backgrounds", column)
