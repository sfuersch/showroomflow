"""Add carry-over credit top-ups and grant audit log."""

from alembic import op
import sqlalchemy as sa


revision = "0008_carryover_credit_topups"
down_revision = "0007_image_service_credits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dealerships",
        sa.Column(
            "additional_vehicle_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "vehicle_credit_usages",
        sa.Column(
            "credit_source",
            sa.String(length=32),
            nullable=False,
            server_default="monthly",
        ),
    )
    op.create_table(
        "vehicle_credit_grants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("dealership_id", sa.Uuid(), nullable=False),
        sa.Column("granted_by_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["dealership_id"], ["dealerships.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["granted_by_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_vehicle_credit_grants_dealership_id"),
        "vehicle_credit_grants",
        ["dealership_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_vehicle_credit_grants_granted_by_id"),
        "vehicle_credit_grants",
        ["granted_by_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_vehicle_credit_grants_granted_by_id"),
        table_name="vehicle_credit_grants",
    )
    op.drop_index(
        op.f("ix_vehicle_credit_grants_dealership_id"),
        table_name="vehicle_credit_grants",
    )
    op.drop_table("vehicle_credit_grants")
    op.drop_column("vehicle_credit_usages", "credit_source")
    op.drop_column("dealerships", "additional_vehicle_credits")
