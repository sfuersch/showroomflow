"""Add configurable SFTP archive filename template.

Revision ID: 0034_sftp_filename_template
Revises: 0033_photo_shadow_controls
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034_sftp_filename_template"
down_revision: str | None = "0033_photo_shadow_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dealership_sftp_settings",
        sa.Column(
            "filename_template",
            sa.String(length=255),
            nullable=False,
            server_default="<VIN>.zip",
        ),
    )


def downgrade() -> None:
    op.drop_column("dealership_sftp_settings", "filename_template")
