"""Add editable OpenAI mask prompt template.

Revision ID: 0037_editable_openai_mask_prompt
Revises: 0036_ftps_cert_fingerprint
"""

from alembic import op
import sqlalchemy as sa


revision = "0037_editable_openai_mask_prompt"
down_revision = "0036_ftps_cert_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "system_image_settings",
        sa.Column("openai_mask_prompt_template", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("system_image_settings", "openai_mask_prompt_template")
