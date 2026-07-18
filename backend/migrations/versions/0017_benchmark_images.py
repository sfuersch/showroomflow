"""Add optional benchmark images for manual quality comparisons."""

from alembic import op
import sqlalchemy as sa


revision = "0017_benchmark_images"
down_revision = "0016_capture_completion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "photo_assets",
        sa.Column("benchmark_object_key", sa.String(length=500), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_assets_benchmark_object_key",
        "photo_assets",
        ["benchmark_object_key"],
    )
    op.add_column(
        "photo_assets",
        sa.Column("benchmark_thumbnail_object_key", sa.String(length=500), nullable=True),
    )
    op.create_unique_constraint(
        "uq_photo_assets_benchmark_thumbnail_object_key",
        "photo_assets",
        ["benchmark_thumbnail_object_key"],
    )
    op.add_column(
        "photo_assets",
        sa.Column("benchmark_content_type", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "photo_assets",
        sa.Column("benchmark_size_bytes", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("photo_assets", "benchmark_size_bytes")
    op.drop_column("photo_assets", "benchmark_content_type")
    op.drop_constraint(
        "uq_photo_assets_benchmark_thumbnail_object_key", "photo_assets", type_="unique"
    )
    op.drop_column("photo_assets", "benchmark_thumbnail_object_key")
    op.drop_constraint(
        "uq_photo_assets_benchmark_object_key", "photo_assets", type_="unique"
    )
    op.drop_column("photo_assets", "benchmark_object_key")
