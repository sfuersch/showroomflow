"""Add activation state to dealerships and locations."""

from alembic import op
import sqlalchemy as sa

revision = "0002_active_tenant_entities"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "dealerships",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.add_column(
        "locations",
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.alter_column("dealerships", "is_active", server_default=None)
    op.alter_column("locations", "is_active", server_default=None)


def downgrade() -> None:
    op.drop_column("locations", "is_active")
    op.drop_column("dealerships", "is_active")
