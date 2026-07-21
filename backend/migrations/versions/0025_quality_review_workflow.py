"""Add operator accounts and quality review workflow metadata."""

from alembic import op
import sqlalchemy as sa


revision = "0025_quality_review_workflow"
down_revision = "0024_interior_background_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLAlchemy stores Enum member names in PostgreSQL for this model.
    op.execute("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'OPERATOR'")
    op.add_column("photo_assets", sa.Column("quality_score", sa.Integer(), nullable=True))
    op.add_column("photo_assets", sa.Column("quality_issues", sa.JSON(), nullable=True))
    op.add_column(
        "photo_assets", sa.Column("quality_model_version", sa.String(length=100), nullable=True)
    )
    op.add_column(
        "photo_assets", sa.Column("quality_review_created_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "photo_assets", sa.Column("quality_reviewed_by_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        "ix_photo_assets_quality_reviewed_by_id",
        "photo_assets",
        ["quality_reviewed_by_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_photo_assets_quality_reviewed_by_id_users",
        "photo_assets",
        "users",
        ["quality_reviewed_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "photo_assets", sa.Column("quality_reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "photo_assets",
        sa.Column("quality_review_resolution", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("photo_assets", "quality_review_resolution")
    op.drop_column("photo_assets", "quality_reviewed_at")
    op.drop_constraint(
        "fk_photo_assets_quality_reviewed_by_id_users", "photo_assets", type_="foreignkey"
    )
    op.drop_index("ix_photo_assets_quality_reviewed_by_id", table_name="photo_assets")
    op.drop_column("photo_assets", "quality_reviewed_by_id")
    op.drop_column("photo_assets", "quality_review_created_at")
    op.drop_column("photo_assets", "quality_model_version")
    op.drop_column("photo_assets", "quality_issues")
    op.drop_column("photo_assets", "quality_score")
    # PostgreSQL enum values cannot safely be removed while rows may reference them.
