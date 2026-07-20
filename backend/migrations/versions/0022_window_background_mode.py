"""Use the window background mode for steering-wheel photos."""

from alembic import op
import sqlalchemy as sa


revision = "0022_window_background_mode"
down_revision = "0021_background_composition"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE orientations "
            "SET processing_mode = 'window_background', requires_processing = true "
            "WHERE key = 'steering-wheel'"
        )
    )
    op.execute(
        sa.text(
            "DELETE FROM background_orientation_compositions "
            "WHERE orientation_id IN ("
            "SELECT id FROM orientations WHERE key = 'steering-wheel'"
            ")"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE orientations "
            "SET processing_mode = 'optimized', requires_processing = true "
            "WHERE key = 'steering-wheel'"
        )
    )
