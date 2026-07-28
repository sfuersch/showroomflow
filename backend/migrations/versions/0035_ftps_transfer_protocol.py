"""Add selectable SFTP or FTPS transfer protocol.

Revision ID: 0035_ftps_transfer_protocol
Revises: 0034_sftp_filename_template
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision: str = "0035_ftps_transfer_protocol"
down_revision: str | None = "0034_sftp_filename_template"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dealership_sftp_settings",
        sa.Column(
            "protocol",
            sa.String(length=16),
            nullable=False,
            server_default="sftp",
        ),
    )


def downgrade() -> None:
    op.drop_column("dealership_sftp_settings", "protocol")
