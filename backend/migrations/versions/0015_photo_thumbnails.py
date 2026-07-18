"""Add thumbnail object keys for photos and processing variants."""

from alembic import op
import sqlalchemy as sa


revision = "0015_photo_thumbnails"
down_revision = "0014_automatic_contour_scaling"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "photo_assets",
        sa.Column("original_thumbnail_object_key", sa.String(length=500), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_assets_original_thumbnail_object_key",
        "photo_assets",
        ["original_thumbnail_object_key"],
    )
    op.add_column(
        "photo_assets",
        sa.Column("processed_thumbnail_object_key", sa.String(length=500), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_assets_processed_thumbnail_object_key",
        "photo_assets",
        ["processed_thumbnail_object_key"],
    )
    op.add_column(
        "photo_processing_variants",
        sa.Column("thumbnail_object_key", sa.String(length=500), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_processing_variants_thumbnail_object_key",
        "photo_processing_variants",
        ["thumbnail_object_key"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_photo_processing_variants_thumbnail_object_key",
        "photo_processing_variants",
        type_="unique",
    )
    op.drop_column("photo_processing_variants", "thumbnail_object_key")
    op.drop_constraint(
        "uq_photo_assets_processed_thumbnail_object_key", "photo_assets", type_="unique"
    )
    op.drop_column("photo_assets", "processed_thumbnail_object_key")
    op.drop_constraint(
        "uq_photo_assets_original_thumbnail_object_key", "photo_assets", type_="unique"
    )
    op.drop_column("photo_assets", "original_thumbnail_object_key")
