"""Add editable semantic mask prompts to orientations."""

from alembic import op
import sqlalchemy as sa


revision = "0029_orientation_mask_prompts"
down_revision = "0028_async_mask_refinement"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orientations", sa.Column("mask_prompt", sa.Text(), nullable=True))
    op.add_column(
        "orientations", sa.Column("mask_negative_prompt", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orientations", "mask_negative_prompt")
    op.drop_column("orientations", "mask_prompt")
