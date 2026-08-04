"""Add an image provider selection to every orientation.

Revision ID: 0039_orientation_processing_provider
Revises: 0038_exterior_360_capture
"""

from alembic import op
import sqlalchemy as sa


revision = "0039_orientation_processing_provider"
down_revision = "0038_exterior_360_capture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "orientations",
        sa.Column("processing_provider", sa.String(length=32), nullable=True),
    )
    op.execute(
        """
        UPDATE orientations
        SET processing_provider = CASE
            WHEN processing_mode = 'original' THEN 'original'
            ELSE COALESCE(
                (
                    SELECT provider
                    FROM system_image_settings
                    WHERE id = 1
                      AND provider IN ('photoroom', 'remove_bg')
                ),
                'photoroom'
            )
        END
        """
    )
    op.alter_column(
        "orientations",
        "processing_provider",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default=sa.text("'photoroom'"),
    )


def downgrade() -> None:
    op.drop_column("orientations", "processing_provider")
