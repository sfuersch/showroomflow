"""Add optional FTPS certificate fingerprint pinning.

Revision ID: 0036_ftps_certificate_fingerprint
Revises: 0035_ftps_transfer_protocol
Create Date: 2026-07-29
"""

import sqlalchemy as sa
from alembic import op


revision: str = "0036_ftps_certificate_fingerprint"
down_revision: str | None = "0035_ftps_transfer_protocol"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "dealership_sftp_settings",
        sa.Column(
            "tls_certificate_fingerprint",
            sa.String(length=128),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "dealership_sftp_settings",
        "tls_certificate_fingerprint",
    )
