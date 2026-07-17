"""Add provider-specific image processing comparison variants."""

from alembic import op
import sqlalchemy as sa


revision = "0006_processing_variants"
down_revision = "0005_image_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "photo_processing_variants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("photo_asset_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("object_key", sa.String(length=500), nullable=True),
        sa.Column("content_type", sa.String(length=100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.String(length=1000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["photo_asset_id"], ["photo_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint(
            "photo_asset_id",
            "provider",
            name="uq_photo_processing_variant_provider",
        ),
    )
    op.create_index(
        op.f("ix_photo_processing_variants_photo_asset_id"),
        "photo_processing_variants",
        ["photo_asset_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_photo_processing_variants_photo_asset_id"),
        table_name="photo_processing_variants",
    )
    op.drop_table("photo_processing_variants")
