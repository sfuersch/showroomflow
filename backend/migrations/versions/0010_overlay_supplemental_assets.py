"""Add dealership overlays and supplemental export images."""

from alembic import op
import sqlalchemy as sa


revision = "0010_overlay_supplemental_assets"
down_revision = "0009_comparison_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "image_overlays",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dealership_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("position", sa.String(length=32), nullable=False),
        sa.Column("width_percent", sa.Integer(), nullable=False),
        sa.Column("opacity_percent", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dealership_id"], ["dealerships.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index(op.f("ix_image_overlays_brand_id"), "image_overlays", ["brand_id"])
    op.create_index(op.f("ix_image_overlays_dealership_id"), "image_overlays", ["dealership_id"])
    op.create_table(
        "image_overlay_locations",
        sa.Column("overlay_id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["overlay_id"], ["image_overlays.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("overlay_id", "location_id"),
    )
    op.create_table(
        "image_overlay_capture_steps",
        sa.Column("overlay_id", sa.Uuid(), nullable=False),
        sa.Column("capture_step_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["capture_step_id"], ["capture_steps.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["overlay_id"], ["image_overlays.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("overlay_id", "capture_step_id"),
    )
    op.create_table(
        "supplemental_images",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dealership_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("export_order", sa.Integer(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["brand_id"], ["brands.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["dealership_id"], ["dealerships.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_index(op.f("ix_supplemental_images_brand_id"), "supplemental_images", ["brand_id"])
    op.create_index(
        op.f("ix_supplemental_images_dealership_id"),
        "supplemental_images",
        ["dealership_id"],
    )
    op.create_table(
        "supplemental_image_locations",
        sa.Column("supplemental_image_id", sa.Uuid(), nullable=False),
        sa.Column("location_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["location_id"], ["locations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["supplemental_image_id"], ["supplemental_images.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("supplemental_image_id", "location_id"),
    )


def downgrade() -> None:
    op.drop_table("supplemental_image_locations")
    op.drop_index(op.f("ix_supplemental_images_dealership_id"), table_name="supplemental_images")
    op.drop_index(op.f("ix_supplemental_images_brand_id"), table_name="supplemental_images")
    op.drop_table("supplemental_images")
    op.drop_table("image_overlay_capture_steps")
    op.drop_table("image_overlay_locations")
    op.drop_index(op.f("ix_image_overlays_dealership_id"), table_name="image_overlays")
    op.drop_index(op.f("ix_image_overlays_brand_id"), table_name="image_overlays")
    op.drop_table("image_overlays")
