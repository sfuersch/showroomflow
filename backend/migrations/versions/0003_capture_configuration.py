"""Add brand, background and capture sequence configuration."""

from alembic import op
import sqlalchemy as sa

revision = "0003_capture_configuration"
down_revision = "0002_active_tenant_entities"
branch_labels = None
depends_on = None


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "brands",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dealership_id", sa.Uuid(), sa.ForeignKey("dealerships.id"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("dealership_id", "name", name="uq_brand_dealership_name"),
    )
    op.create_index("ix_brands_dealership_id", "brands", ["dealership_id"])
    op.create_table(
        "backgrounds",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dealership_id", sa.Uuid(), sa.ForeignKey("dealerships.id"), nullable=False),
        sa.Column("brand_id", sa.Uuid(), sa.ForeignKey("brands.id"), nullable=True),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("object_key", sa.String(500), nullable=False, unique=True),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        *_timestamps(),
    )
    op.create_index("ix_backgrounds_dealership_id", "backgrounds", ["dealership_id"])
    op.create_index("ix_backgrounds_brand_id", "backgrounds", ["brand_id"])
    op.add_column(
        "vehicle_jobs",
        sa.Column("brand_id", sa.Uuid(), sa.ForeignKey("brands.id"), nullable=True),
    )
    op.add_column(
        "vehicle_jobs",
        sa.Column("background_id", sa.Uuid(), sa.ForeignKey("backgrounds.id"), nullable=True),
    )
    op.create_index("ix_vehicle_jobs_brand_id", "vehicle_jobs", ["brand_id"])
    op.create_index("ix_vehicle_jobs_background_id", "vehicle_jobs", ["background_id"])
    op.create_table(
        "background_locations",
        sa.Column(
            "background_id",
            sa.Uuid(),
            sa.ForeignKey("backgrounds.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "location_id",
            sa.Uuid(),
            sa.ForeignKey("locations.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_table(
        "capture_steps",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("dealership_id", sa.Uuid(), sa.ForeignKey("dealerships.id"), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("instruction", sa.String(500), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("capture_order", sa.Integer(), nullable=False),
        sa.Column("export_order", sa.Integer(), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("requires_processing", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("silhouette_object_key", sa.String(500), nullable=True),
        sa.Column("silhouette_content_type", sa.String(100), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("dealership_id", "name", name="uq_capture_step_dealership_name"),
    )
    op.create_index("ix_capture_steps_dealership_id", "capture_steps", ["dealership_id"])


def downgrade() -> None:
    op.drop_table("capture_steps")
    op.drop_table("background_locations")
    op.drop_index("ix_vehicle_jobs_background_id", table_name="vehicle_jobs")
    op.drop_index("ix_vehicle_jobs_brand_id", table_name="vehicle_jobs")
    op.drop_column("vehicle_jobs", "background_id")
    op.drop_column("vehicle_jobs", "brand_id")
    op.drop_table("backgrounds")
    op.drop_table("brands")
