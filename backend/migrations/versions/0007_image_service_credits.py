"""Add image service settings and monthly vehicle credits."""

from alembic import op
import sqlalchemy as sa


revision = "0007_image_service_credits"
down_revision = "0006_processing_variants"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dealerships",
        sa.Column(
            "monthly_vehicle_credits",
            sa.Integer(),
            nullable=False,
            server_default="30",
        ),
    )
    op.add_column(
        "photo_assets",
        sa.Column("processed_provider", sa.String(length=32), nullable=True),
    )
    settings_table = op.create_table(
        "system_image_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("photoroom_sandbox", sa.Boolean(), nullable=False),
        sa.Column(
            "default_monthly_vehicle_credits",
            sa.Integer(),
            nullable=False,
        ),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.bulk_insert(
        settings_table,
        [
            {
                "id": 1,
                "provider": "remove_bg",
                "photoroom_sandbox": True,
                "default_monthly_vehicle_credits": 30,
            }
        ],
    )
    op.create_table(
        "vehicle_credit_usages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dealership_id", sa.Uuid(), nullable=False),
        sa.Column("vehicle_job_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dealership_id"], ["dealerships.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["vehicle_job_id"], ["vehicle_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_vehicle_credit_usages_dealership_id"),
        "vehicle_credit_usages",
        ["dealership_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vehicle_credit_usages_period_start"),
        "vehicle_credit_usages",
        ["period_start"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vehicle_credit_usages_vehicle_job_id"),
        "vehicle_credit_usages",
        ["vehicle_job_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_vehicle_credit_usages_vehicle_job_id"),
        table_name="vehicle_credit_usages",
    )
    op.drop_index(
        op.f("ix_vehicle_credit_usages_period_start"),
        table_name="vehicle_credit_usages",
    )
    op.drop_index(
        op.f("ix_vehicle_credit_usages_dealership_id"),
        table_name="vehicle_credit_usages",
    )
    op.drop_table("vehicle_credit_usages")
    op.drop_table("system_image_settings")
    op.drop_column("photo_assets", "processed_provider")
    op.drop_column("dealerships", "monthly_vehicle_credits")
