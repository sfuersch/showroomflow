"""Add Web Push subscriptions and quality-review notification state."""

from alembic import op
import sqlalchemy as sa


revision = "0027_web_push_notifications"
down_revision = "0026_external_api_usage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "photo_assets",
        sa.Column("quality_review_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("p256dh", sa.String(length=255), nullable=False),
        sa.Column("auth", sa.String(length=255), nullable=False),
        sa.Column("user_agent", sa.String(length=500), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint"),
    )
    op.create_index(
        "ix_web_push_subscriptions_user_id",
        "web_push_subscriptions",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("web_push_subscriptions")
    op.drop_column("photo_assets", "quality_review_notified_at")
